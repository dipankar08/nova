# vim: tabstop=4 shiftwidth=4 softtabstop=4

# Copyright (c) 2010 Openstack, LLC.
# Copyright 2010 United States Government as represented by the
# Administrator of the National Aeronautics and Space Administration.
# All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

"""
Scheduler base class that all Schedulers should inherit from
"""

import datetime

from nova import db
from nova import exception
from nova import flags
from nova import log as logging
from nova import rpc
from nova.compute import power_state

FLAGS = flags.FLAGS
flags.DEFINE_integer('service_down_time', 60,
                     'maximum time since last checkin for up service')


class NoValidHost(exception.Error):
    """There is no valid host for the command."""
    pass


class WillNotSchedule(exception.Error):
    """The specified host is not up or doesn't exist."""
    pass


class Scheduler(object):
    """The base class that all Scheduler clases should inherit from."""

    @staticmethod
    def service_is_up(service):
        """Check whether a service is up based on last heartbeat."""
        last_heartbeat = service['updated_at'] or service['created_at']
        # Timestamps in DB are UTC.
        elapsed = datetime.datetime.utcnow() - last_heartbeat
        return elapsed < datetime.timedelta(seconds=FLAGS.service_down_time)

    def hosts_up(self, context, topic):
        """Return the list of hosts that have a running service for topic."""

        services = db.service_get_all_by_topic(context, topic)
        return [service.host
                for service in services
                if self.service_is_up(service)]

    def schedule(self, context, topic, *_args, **_kwargs):
        """Must override at least this method for scheduler to work."""
        raise NotImplementedError(_("Must implement a fallback schedule"))

    def schedule_live_migration(self, context, instance_id, dest):
        """ live migration method """

        # Whether instance exists and running
        instance_ref = db.instance_get(context, instance_id)
        ec2_id = instance_ref['hostname']

        # Checking instance state.
        if power_state.RUNNING != instance_ref['state'] or \
           'running' != instance_ref['state_description']:
            msg = _('Instance(%s) is not running')
            raise exception.Invalid(msg % ec2_id)

        # Checking destination host exists
        dhost_ref = db.host_get_by_name(context, dest)

        # Checking whether The host where instance is running
        # and dest is not same.
        src = instance_ref['host']
        if dest == src:
            msg = _('%s is where %s is running now. choose other host.')
            raise exception.Invalid(msg % (dest, ec2_id))

        # Checking dest is compute node.
        services = db.service_get_all_by_topic(context, 'compute')
        if dest not in [service.host for service in services]:
            msg = _('%s must be compute node')
            raise exception.Invalid(msg % dest)

        # Checking dest host is alive.
        service = [service for service in services if service.host == dest]
        service = service[0]
        if not self.service_is_up(service):
            msg = _('%s is not alive(time synchronize problem?)')
            raise exception.Invalid(msg % dest)

        # NOTE(masumotok): Below pre-checkings are followed by
        # http://wiki.libvirt.org/page/TodoPreMigrationChecks

        # Checking hypervisor is same.
        orighost = instance_ref['launched_on']
        ohost_ref = db.host_get_by_name(context, orighost)

        otype = ohost_ref['hypervisor_type']
        dtype = dhost_ref['hypervisor_type']
        if otype != dtype:
            msg = _('Different hypervisor type(%s->%s)')
            raise exception.Invalid(msg % (otype, dtype))

        # Checkng hypervisor version.
        oversion = ohost_ref['hypervisor_version']
        dversion = dhost_ref['hypervisor_version']
        if oversion > dversion:
            msg = _('Older hypervisor version(%s->%s)')
            raise exception.Invalid(msg % (oversion, dversion))

        # Checking cpuinfo.
        cpuinfo = ohost_ref['cpu_info']
        if str != type(cpuinfo):
            msg = _('Unexpected err: not found cpu_info for %s on DB.hosts')
            raise exception.Invalid(msg % orighost)

        try:
            rpc.call(context,
                 db.queue_get_for(context, FLAGS.compute_topic, dest),
                 {"method": 'compare_cpu',
                  "args": {'xml': cpuinfo}})

        except rpc.RemoteError, e:
            msg = '%s doesnt have compatibility to %s(where %s launching at)\n'
            msg += 'result:%s \n'
            logging.error(_(msg) % (dest, src, ec2_id, ret))
            raise e

        # Checking dst host still has enough capacities.
        self.has_enough_resource(context, instance_id, dest)

        # Changing instance_state.
        db.instance_set_state(context,
                              instance_id,
                              power_state.PAUSED,
                              'migrating')

        # Changing volume state
        try:
            for vol in db.volume_get_all_by_instance(context, instance_id):
                db.volume_update(context,
                                 vol['id'],
                                 {'status': 'migrating'})
        except exception.NotFound:
            pass

        # Return value is necessary to send request to src
        # Check _schedule() in detail.
        return src

    def has_enough_resource(self, context, instance_id, dest):
        """ Check if destination host has enough resource for live migration"""

        # Getting instance information
        instance_ref = db.instance_get(context, instance_id)
        ec2_id = instance_ref['hostname']
        vcpus = instance_ref['vcpus']
        mem = instance_ref['memory_mb']
        hdd = instance_ref['local_gb']

        # Gettin host information
        host_ref = db.host_get_by_name(context, dest)
        total_cpu = int(host_ref['vcpus'])
        total_mem = int(host_ref['memory_mb'])
        total_hdd = int(host_ref['local_gb'])

        instances_ref = db.instance_get_all_by_host(context, dest)
        for i_ref in instances_ref:
            total_cpu -= int(i_ref['vcpus'])
            total_mem -= int(i_ref['memory_mb'])
            total_hdd -= int(i_ref['local_gb'])

        # Checking host has enough information
        logging.debug('host(%s) remains vcpu:%s mem:%s hdd:%s,' %
                      (dest, total_cpu, total_mem, total_hdd))
        logging.debug('instance(%s) has vcpu:%s mem:%s hdd:%s,' %
                      (ec2_id, vcpus, mem, hdd))

        if total_cpu <= vcpus or total_mem <= mem or total_hdd <= hdd:
            msg = '%s doesnt have enough resource for %s' % (dest, ec2_id)
            raise exception.NotEmpty(msg)

        logging.debug(_('%s has enough resource for %s') % (dest, ec2_id))
