"""
A toolkit for identifying and advertising service resources.

Uses a specific naming convention for the Task Definition of services.  If you
name the Task Definition ending with "-service", no configuration is needed.
This also requires that you not use that naming convention for task definitions
that are not services.

For example:
    A Task Definition with the family name of 'cache-service' will have its
    hosting Container Instance's internal ip added to a Route53 private Zone as
    cache.local and other machines on the same subnet can address it that way.
"""

import argparse
import os
import re
import boto

ecs = boto.connect_ec2containerservice()
ec2 = boto.connect_ec2()
route53 = boto.connect_route53()

if 'ECS_CLUSTER' in os.environ:
    cluster = os.environ['ECS_CLUSTER']
elif os.path.exists('/etc/ecs/ecs.config'):
    pat = re.compile(r'\bECS_CLUSTER\b\s*=\s*(\w*)')
    cluster = pat.findall(open('/etc/ecs/ecs.config').read())[-1]
else:
    cluster = None


class MultipleTasksRunningForService(Exception):

    """It's assumed that there should be only 1 task running for a service.

    Am I wrong? Tell me.
    """

    pass


def get_task_definition_arns():
    """Request all API pages needed to get Task Definition ARNS."""
    next_token = []
    arns = []
    while next_token is not None:
        detail = ecs.list_task_definitions(next_token=next_token)
        detail = detail['ListTaskDefinitionsResponse']
        detail = detail['ListTaskDefinitionsResult']
        arns.extend(detail['taskDefinitionArns'])
        next_token = detail['nextToken']
    return arns


def get_task_definition_families():
    """Ignore duplicate tasks in the same family."""
    arns = get_task_definition_arns()
    families = {}
    for arn in arns:
        match = pattern_arn.match(arn)
        if match:
            groupdict = match.groupdict()
            families[groupdict['family']] = True
    return families.keys()


def get_task_arn(family):
    """Get the ARN of running task, given the family name."""
    response = ecs.list_tasks(cluster=cluster, family=family)
    arns = response['ListTasksResponse']['ListTasksResult']['taskArns']
    if len(arns) == 0:
        return None
    if len(arns) > 1:
        raise MultipleTasksRunningForService
    return arns[0]


def get_task_container_instance_arn(task_arn):
    """Get the ARN for the container instance a give task is running on."""
    response = ecs.describe_tasks(task_arn, cluster=cluster)
    response = response['DescribeTasksResponse']
    return response['DescribeTasksResult']['tasks'][0]['containerInstanceArn']


def get_container_instance_ec2_instance_id(container_instance_arn):
    """Id the EC2 instance serving as the container instance."""
    detail = ecs.describe_container_instances(
        container_instances=container_instance_arn, cluster=cluster)
    detail = detail['DescribeContainerInstancesResponse']
    detail = detail['DescribeContainerInstancesResult']['containerInstances']
    return detail[0]['ec2InstanceId']


def get_ec2_interface(ec2_instance_id):
    """Get the primary interface for the given EC2 instance."""
    return ec2.get_all_instances(filters={
        'instance-id': ec2_instance_id})[0].instances[0].interfaces[0]


def get_zone_for_vpc(vpc_id):
    """Identify the Hosted Zone for the given VPC.

    Assumes a 1 to 1 relationship.

    NOTE: There is an existing bug.
    https://github.com/boto/boto/issues/3061
    When that changes, I expect to have to search ['VPCs'] as a list of
    dictionaries rather than a dictionary.  This has the unfortunate side
    effect of not working for Hosted Zones that are associated with more than
    one VPC. (But, why would you expect internal DNS for 2 different private
    networks to be the same anyway?)
    """
    response = route53.get_all_hosted_zones()['ListHostedZonesResponse']
    for zone in response['HostedZones']:
        zone_id = zone['Id'].split('/')[-1]
        detail = route53.get_hosted_zone(zone_id)['GetHostedZoneResponse']
        if detail['VPCs']['VPC']['VPCId'] == vpc_id:
            return {'zone_id': zone_id, 'zone_name': zone['Name']}


def get_info():
    """Get all needed info about running services."""
    info = {'services': [], 'network': {'cluster': cluster}}
    families = get_task_definition_families()
    for family in families:
        service_dict = {}
        if family[-8:] != '-service':
            continue
        service_dict['service_arn'] = get_task_arn(family)
        if not service_dict['service_arn']:
            # task is not running, skip it
            continue
        service_dict['family'] = family
        service_dict['name'] = family[:-8]
        service_dict['container_instance_arn'] = (
            get_task_container_instance_arn(service_dict['service_arn']))
        service_dict['ec2_instance_id'] = (
            get_container_instance_ec2_instance_id(
                service_dict['container_instance_arn']))
        ec2_interface = get_ec2_interface(service_dict['ec2_instance_id'])
        service_dict['container_instance_internal_ip'] = (
            ec2_interface.private_ip_address)
        # No need to get common network info on each loop over tasks
        if 'vpc_id' not in info['network']:
            info['network']['vpc_id'] = ec2_interface.vpc_id
            info['network'].update(get_zone_for_vpc(info['network']['vpc_id']))
        info['services'].append(service_dict)
    return info


def dns(zone_id, zone_name, service_name, service_ip, ttl=20):
    """Insert or update DNS record."""
    rrs = boto.route53.record.ResourceRecordSets(route53, zone_id)
    rrs.add_change('UPSERT', '{service_name}.{zone_name}'.format(**locals()),
                   'A', ttl).add_value(service_ip)
    rrs.commit()
    return rrs


def update_services(service_names=[], verbose=False):
    """Update DNS to allow discovery of properly named task definitions.

    If service_names are provided only update those services.
    Otherwise update all.
    """
    info = get_info()
    for service in info['services']:
        if (service_names and
                service['family'] not in service_names and
                service['name'] not in service_names):
            continue
        if verbose:
            print "registering {0}".format(service['name'])
        dns(info['network']['zone_id'], info['network']['zone_name'],
            service['name'], service['container_instance_internal_ip'])

def cli():
    parser = argparse.ArgumentParser()
    parser.add_argument('service_names', nargs='*',
                        help='list of services to start')
    update_services(parser.parse_args().service_names, True)

pattern_arn = re.compile(
    'arn:'
    '(?P<partition>[^:]+):'
    '(?P<service>[^:]+):'
    '(?P<region>[^:]*):'   # region is optional
    '(?P<account>[^:]*):'  # account is optional
    '(?P<resourcetype>[^:/]+)([:/])'
    '(?P<resource>('
        '(?P<family>[^:]+):'     # noqa
        '(?P<version>[^:]+)|.*'  # noqa
    '))')
