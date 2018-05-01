import copy
import json
import os
import re
import shutil
from tempfile import mkdtemp

from cloud_inquisitor import get_aws_session
from cloud_inquisitor.config import dbconfig, ConfigOption
from cloud_inquisitor.constants import NS_AUDITOR_IAM, AccountTypes
from cloud_inquisitor.database import db
from cloud_inquisitor.plugins import BaseAuditor
from cloud_inquisitor.schema import Account, AuditLog
from cloud_inquisitor.wrappers import retry
from git import Repo


class IAMAuditor(BaseAuditor):
    """Validate and apply IAM policies for AWS Accounts
    """
    name = 'IAM'
    ns = NS_AUDITOR_IAM
    interval = dbconfig.get('interval', ns, 30)
    start_delay = 0
    manage_roles = dbconfig.get('manage_roles', ns, True)
    git_policies = None
    cfg_roles = None
    aws_managed_policies = None
    options = (
        ConfigOption('enabled', False, 'bool', 'Enable the IAM roles and policy auditor'),
        ConfigOption('interval', 30, 'int', 'How often the auditor executes, in minutes'),
        ConfigOption('manage_roles', True, 'bool', 'Enable management of IAM roles'),
        ConfigOption('roles', '{ }', 'json',
            'JSON document with roles to push to accounts. See documentation for examples'),
        ConfigOption('delete_inline_policies', False, 'bool', 'Delete inline policies from existing roles'),
        ConfigOption('git_auth_token', 'CHANGE ME', 'string', 'API Auth token for Github'),
        ConfigOption('git_server', 'CHANGE ME', 'string', 'Address of the Github server'),
        ConfigOption('git_repo', 'CHANGE ME', 'string', 'Name of Github repo'),
        ConfigOption('git_no_ssl_verify', False, 'bool', 'Disable SSL verification of Github server'),
        ConfigOption('role_timeout', 8, 'int', 'AssumeRole timeout in hours')
    )

    def run(self, *args, **kwargs):
        """Iterate through all AWS accounts and apply roles and policies from Github

        Args:
            *args: Optional list of arguments
            **kwargs: Optional list of keyword arguments

        Returns:
            `None`
        """
        accounts = db.Account.find(
            Account.enabled == 1,
            Account.account_type == AccountTypes.AWS
        )
        self.manage_policies(accounts)
        self.update_role_timeouts(accounts)

    def update_role_timeouts(self, accounts):
        if not accounts:
            return
        timeout_in_seconds = self.dbconfig.get('role_timeout_in_hours', self.ns, 8) * 60 * 60
        for account in accounts:
            sess = get_aws_session(account)
            iam = sess.client('iam')
            role_list = iam.list_roles()['Roles']
            for role in role_list:
                if 'service-role' not in role['Arn']:
                    try:
                        if role['MaxSessionDuration'] != timeout_in_seconds:
                            iam.update_role(RoleName=role['RoleName'], MaxSessionDuration=timeout_in_seconds)
                            self.log.info(
                                'Adjusted MaxSessionDuration for role {} in account {} to {} seconds'
                                .format(role['RoleName'], account.account_name, timeout_in_seconds)
                            )
                    except Exception as error:
                        self.log.exception(
                            'Unable to adjust MaxSessionDuration for role {} in account {}'
                            .format(role['RoleName'], account.account_name)
                        )
                else:
                    self.log.info(
                        'Role {} in account {} is a service linked role and cannot be modified.'
                        .format(role['RoleName'], account.account_name)
                    )

    def manage_policies(self, accounts):
        if not accounts:
            return

        self.git_policies = self.get_policies_from_git()
        self.manage_roles = self.dbconfig.get('manage_roles', self.ns, True)
        self.cfg_roles = self.dbconfig.get('roles', self.ns)
        self.aws_managed_policies = {policy['PolicyName']: policy for policy in self.get_policies_from_aws(
            get_aws_session(accounts[0]).client('iam'),
            'AWS'
        )}

        for account in accounts:
            try:
                if not account.ad_group_base:
                    self.log.info('Account {} does not have AD Group Base set, skipping'.format(account.account_name))
                    continue

                # List all policies and roles from AWS, and generate a list of policies from Git
                sess = get_aws_session(account)
                iam = sess.client('iam')

                aws_roles = {role['RoleName']: role for role in self.get_roles(iam)}
                aws_policies = {policy['PolicyName']: policy for policy in self.get_policies_from_aws(iam)}

                account_policies = copy.deepcopy(self.git_policies['GLOBAL'])

                if account.account_name in self.git_policies:
                    for role in self.git_policies[account.account_name]:
                        account_policies.update(self.git_policies[account.account_name][role])

                aws_policies.update(self.check_policies(account, account_policies, aws_policies))
                self.check_roles(account, aws_policies, aws_roles)
            except Exception as exception:
                self.log.info('Unable to process account {}. Unhandled Exception {}'.format(
                    account.account_name, exception))

    @retry
    def check_policies(self, account, account_policies, aws_policies):
        """Iterate through the policies of a specific account and create or update the policy if its missing or
        does not match the policy documents from Git. Returns a dict of all the policies added to the account
        (does not include updated policies)

        Args:
            account (:obj:`Account`): Account to check policies for
            account_policies (`dict` of `str`: `dict`): A dictionary containing all the policies for the specific
            account
            aws_policies (`dict` of `str`: `dict`): A dictionary containing the non-AWS managed policies on the account

        Returns:
            :obj:`dict` of `str`: `str`
        """
        self.log.debug('Fetching policies for {}'.format(account.account_name))
        sess = get_aws_session(account)
        iam = sess.client('iam')
        added = {}

        for policyName, account_policy in account_policies.items():
            # policies pulled from github a likely bytes and need to be converted
            if isinstance(account_policy, bytes):
                account_policy = account_policy.decode('utf-8')

            # Using re.sub instead of format since format breaks on the curly braces of json
            gitpol = json.loads(
                re.sub(
                    r'\{AD_Group\}',
                    account.ad_group_base or account.account_name,
                    account_policy
                )
            )

            if policyName in aws_policies:
                pol = aws_policies[policyName]
                awspol = iam.get_policy_version(
                    PolicyArn=pol['Arn'],
                    VersionId=pol['DefaultVersionId']
                )['PolicyVersion']['Document']

                if awspol != gitpol:
                    self.log.warn('IAM Policy {} on {} does not match Git policy documents, updating'.format(
                        policyName,
                        account.account_name
                    ))

                    self.create_policy(account, iam, json.dumps(gitpol, indent=4), policyName, arn=pol['Arn'])
                else:
                    self.log.debug('IAM Policy {} on {} is up to date'.format(
                        policyName,
                        account.account_name
                    ))
            else:
                self.log.warn('IAM Policy {} is missing on {}'.format(policyName, account.account_name))
                response = self.create_policy(account, iam, json.dumps(gitpol), policyName)
                added[policyName] = response['Policy']

        return added

    @retry
    def check_roles(self, account, aws_policies, aws_roles):
        """Iterate through the roles of a specific account and create or update the roles if they're missing or
        does not match the roles from Git.

        Args:
            account (:obj:`Account`): The account to check roles on
            aws_policies (:obj:`dict` of `str`: `dict`): A dictionary containing all the policies for the specific
            account
            aws_roles (:obj:`dict` of `str`: `dict`): A dictionary containing all the roles for the specific account

        Returns:
            `None`
        """
        self.log.debug('Checking roles for {}'.format(account.account_name))
        sess = get_aws_session(account)
        iam = sess.client('iam')

        # Build a list of default role policies and extra account specific role policies
        account_roles = copy.deepcopy(self.cfg_roles)
        if account.account_name in self.git_policies:
            for role in self.git_policies[account.account_name]:
                if role in account_roles:
                    account_roles[role]['policies'] += list(self.git_policies[account.account_name][role].keys())

        for roleName, data in list(account_roles.items()):
            if roleName not in aws_roles:
                self.log.info('IAM Role {} is missing on {}'.format(roleName, account.account_name))
                continue

            aws_role_policies = [x['PolicyName'] for x in iam.list_attached_role_policies(
                RoleName=roleName)['AttachedPolicies']
            ]
            aws_role_inline_policies = iam.list_role_policies(RoleName=roleName)['PolicyNames']
            cfg_role_policies = data['policies']

            missing_policies = list(set(cfg_role_policies) - set(aws_role_policies))
            extra_policies = list(set(aws_role_policies) - set(cfg_role_policies))

            if aws_role_inline_policies:
                self.log.info('IAM Role {} on {} has the following inline policies: {}'.format(
                    roleName,
                    account.account_name,
                    ', '.join(aws_role_inline_policies)
                ))

                if self.dbconfig.get('delete_inline_policies', self.ns, False) and self.manage_roles:
                    for policy in aws_role_inline_policies:
                        AuditLog.log(
                            event='iam.check_roles.delete_inline_role_policy',
                            actor=self.ns,
                            data={
                                'account': account.account_name,
                                'roleName': roleName,
                                'policy': policy
                            }
                        )
                        iam.delete_role_policy(RoleName=roleName, PolicyName=policy)

            if missing_policies:
                self.log.info('IAM Role {} on {} is missing the following policies: {}'.format(
                    roleName,
                    account.account_name,
                    ', '.join(missing_policies)
                ))
                if self.manage_roles:
                    for policy in missing_policies:
                        AuditLog.log(
                            event='iam.check_roles.attach_role_policy',
                            actor=self.ns,
                            data={
                                'account': account.account_name,
                                'roleName': roleName,
                                'policyArn': aws_policies[policy]['Arn']
                            }
                        )
                        iam.attach_role_policy(RoleName=roleName, PolicyArn=aws_policies[policy]['Arn'])

            if extra_policies:
                self.log.info('IAM Role {} on {} has the following extra policies applied: {}'.format(
                    roleName,
                    account.account_name,
                    ', '.join(extra_policies)
                ))

                for policy in extra_policies:
                    if policy in aws_policies:
                        polArn = aws_policies[policy]['Arn']
                    elif policy in self.aws_managed_policies:
                        polArn = self.aws_managed_policies[policy]['Arn']
                    else:
                        self.log.info('IAM Role {} on {} has an unknown policy attached: {}'.format(
                            roleName,
                            account.account_name,
                            policy
                        ))

                    if self.manage_roles and polArn:
                        AuditLog.log(
                            event='iam.check_roles.detach_role_policy',
                            actor=self.ns,
                            data={
                                'account': account.account_name,
                                'roleName': roleName,
                                'policyArn': polArn
                            }
                        )
                        iam.detach_role_policy(RoleName=roleName, PolicyArn=polArn)

    def get_policies_from_git(self):
        """Retrieve policies from the Git repo. Returns a dictionary containing all the roles and policies

        Returns:
            :obj:`dict` of `str`: `dict`
        """
        fldr = mkdtemp()
        try:
            url = 'https://{token}:x-oauth-basic@{server}/{repo}'.format(**{
                'token': self.dbconfig.get('git_auth_token', self.ns),
                'server': self.dbconfig.get('git_server', self.ns),
                'repo': self.dbconfig.get('git_repo', self.ns)
            })

            policies = {'GLOBAL': {}}
            if self.dbconfig.get('git_no_ssl_verify', self.ns, False):
                os.environ['GIT_SSL_NO_VERIFY'] = '1'

            repo = Repo.clone_from(url, fldr)
            for obj in repo.head.commit.tree:
                name, ext = os.path.splitext(obj.name)

                # Read the standard policies
                if ext == '.json':
                    policies['GLOBAL'][name] = obj.data_stream.read()

                # Read any account role specific policies
                if name == 'roles' and obj.type == 'tree':
                    for account in [x for x in obj.trees]:
                        for role in [x for x in account.trees]:
                            role_policies = {policy.name.replace('.json', ''): policy.data_stream.read() for policy in
                                             role.blobs if
                                             policy.name.endswith('.json')}

                            if account.name in policies:
                                if role.name in policies[account.name]:
                                    policies[account.name][role.name] += role_policies
                                else:
                                    policies[account.name][role.name] = role_policies
                            else:
                                policies[account.name] = {
                                    role.name: role_policies
                                }

            return policies
        finally:
            if os.path.exists(fldr) and os.path.isdir(fldr):
                shutil.rmtree(fldr)

    @staticmethod
    def get_policies_from_aws(client, scope='Local'):
        """Returns a list of all the policies currently applied to an AWS Account. Returns a list containing all the
        policies for the specified scope

        Args:
            client (:obj:`boto3.session.Session`): A boto3 Session object
            scope (`str`): The policy scope to use. Default: Local

        Returns:
            :obj:`list` of `dict`
        """
        done = False
        marker = None
        policies = []

        while not done:
            if marker:
                response = client.list_policies(Marker=marker, Scope=scope)
            else:
                response = client.list_policies(Scope=scope)

            policies += response['Policies']

            if response['IsTruncated']:
                marker = response['Marker']
            else:
                done = True

        return policies

    @staticmethod
    def get_roles(client):
        """Returns a list of all the roles for an account. Returns a list containing all the roles for the account.

        Args:
            client (:obj:`boto3.session.Session`): A boto3 Session object

        Returns:
            :obj:`list` of `dict`
        """
        done = False
        marker = None
        roles = []

        while not done:
            if marker:
                response = client.list_roles(Marker=marker)
            else:
                response = client.list_roles()

            roles += response['Roles']

            if response['IsTruncated']:
                marker = response['Marker']
            else:
                done = True

        return roles

    def create_policy(self, account, client, document, name, arn=None):
        """Create a new IAM policy.

        If the policy already exists, a new version will be added and if needed the oldest policy version not in use
        will be removed. Returns a dictionary containing the policy or version information

        Args:
            account (:obj:`Account`): Account to create the policy on
            client (:obj:`boto3.client`): A boto3 client object
            document (`str`): Policy document
            name (`str`): Name of the policy to create / update
            arn (`str`): Optional ARN for the policy to update

        Returns:
            `dict`
        """
        AuditLog.log(
            event='iam.check_roles.create_policy',
            actor=self.ns,
            data={
                'account': account.account_name,
                'policyName': name,
                'policyArn': arn
            }
        )
        if not arn and not name:
            raise ValueError('create_policy must be called with either arn or name in the argument list')

        if arn:
            response = client.list_policy_versions(PolicyArn=arn)

            # If we're at the max of the 5 possible versions, remove the oldest version that is not
            # the currently active policy
            if len(response['Versions']) >= 5:
                version = [x for x in sorted(
                    response['Versions'],
                    key=lambda k: k['CreateDate']
                ) if not x['IsDefaultVersion']][0]

                self.log.info('Deleting oldest IAM Policy version {}/{}'.format(arn, version['VersionId']))
                AuditLog.log(
                    event='iam.check_roles.delete_policy_version',
                    actor=self.ns,
                    data={
                        'account': account.account_name,
                        'policyName': name,
                        'policyArn': arn,
                        'versionId': version['VersionId']
                    }
                )
                client.delete_policy_version(PolicyArn=arn, VersionId=version['VersionId'])

            return client.create_policy_version(
                PolicyArn=arn,
                PolicyDocument=document,
                SetAsDefault=True
            )
        else:
            return client.create_policy(
                PolicyName=name,
                PolicyDocument=document
            )
