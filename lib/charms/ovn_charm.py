# Copyright 2019 Canonical Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#  http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
import collections
import ipaddress
import os
import subprocess

import charms.reactive as reactive

import charmhelpers.core as ch_core
import charmhelpers.contrib.charmsupport.nrpe as nrpe
import charmhelpers.contrib.openstack.context as os_context
import charmhelpers.contrib.network.ovs as ch_ovs
import charmhelpers.contrib.network.ovs.ovsdb as ch_ovsdb
import charmhelpers.contrib.openstack.deferred_events as deferred_events

import charms_openstack.adapters
import charms_openstack.charm


CERT_RELATION = 'certificates'
_DEFERABLE_SVC_LIST = ['openvswitch-switch', 'ovn-controller', 'ovn-host',
                       'ovs-vswitchd', 'ovsdb-server', 'ovs-record-hostname']


class DeferredEventMixin():
    """Mixin to add to charm class to add support for deferred events."""

    def restart_on_change(self):
        """Restart the services in the self.restart_map{} attribute if any of
        the files identified by the keys changes for the wrapped call.

        Usage:

           with restart_on_change(restart_map, ...):
               do_stuff_that_might_trigger_a_restart()
               ...
        """
        return ch_core.host.restart_on_change(
            self.full_restart_map,
            stopstart=True,
            restart_functions=getattr(self, 'restart_functions', None),
            can_restart_now_f=deferred_events.check_and_record_restart_request,
            post_svc_restart_f=deferred_events.process_svc_restart)

    @property
    def deferable_services(self):
        """Services which should be stopped from restarting.

        All services from self.services are deferable. But the charm may
        install a package which install a service that the charm does not add
        to its restart_map. In that case it will be missing from
        self.services. However one of the jobs of deferred events is to ensure
        that packages updates outside of charms also do not restart services.
        To ensure there is a complete list take the services from self.services
        and also add in a known list of networking services.

        NOTE: It does not matter if one of the services in the list is not
        installed on the system.

        """
        svcs = self.services[:]
        svcs.extend(_DEFERABLE_SVC_LIST)
        return list(set(svcs))

    def configure_deferred_restarts(self):
        """Install deferred event files and policies.

        Check that the charm supports deferred events by checking for the
        presence of the 'enable-auto-restarts' config option. If it is present
        then install the supporting files and directories, however,
        configure_deferred_restarts only enables deferred events if
        'enable-auto-restarts' is True.
        """
        if 'enable-auto-restarts' in ch_core.hookenv.config().keys():
            deferred_events.configure_deferred_restarts(
                self.deferable_services)
            # Reactive charms execute perm missing.
            os.chmod(
                '/var/lib/charm/{}/policy-rc.d'.format(
                    ch_core.hookenv.service_name()),
                0o755)

    def custom_assess_status_check(self):
        """Report deferred events in charm status message."""
        state = None
        message = None
        deferred_events.check_restart_timestamps()
        events = collections.defaultdict(set)
        for e in deferred_events.get_deferred_events():
            events[e.action].add(e.service)
        for action, svcs in events.items():
            svc_msg = "Services queued for {}: {}".format(
                action, ', '.join(sorted(svcs)))
            state = 'active'
            if message:
                message = "{}. {}".format(message, svc_msg)
            else:
                message = svc_msg
        deferred_hooks = deferred_events.get_deferred_hooks()
        if deferred_hooks:
            state = 'active'
            svc_msg = "Hooks skipped due to disabled auto restarts: {}".format(
                ', '.join(sorted(deferred_hooks)))
            if message:
                message = "{}. {}".format(message, svc_msg)
            else:
                message = svc_msg
        return state, message

    def configure_ovs(self, sb_conn, mlockall_changed,
                      check_deferred_events=True):
        """Run configure_ovs if permitted.

        :param sb_conn: Comma separated string of OVSDB connection methods.
        :type sb_conn: str
        :param mlockall_changed: Whether the mlockall param has changed.
        :type mlockall_changed: bool
        :param check_deferred_events: Whether to check if restarts are
                                      permitted before running hook.
        :type check_deferred_events: bool
        """
        changed = reactive.flags.is_flag_set(
            'config.changed.enable-auto-restarts')
        # Run method if enable-auto-restarts has changed, irrespective of what
        # it was changed to. This ensure that this method is not immediately
        # deferred when enable-auto-restarts is initially set to False.
        if ((not check_deferred_events) or changed or
                deferred_events.is_restart_permitted()):
            deferred_events.clear_deferred_hook('configure_ovs')
            super().configure_ovs(sb_conn, mlockall_changed)
        else:
            deferred_events.set_deferred_hook('configure_ovs')

    def install(self, check_deferred_events=True):
        """Run install if permitted.

        :param check_deferred_events: Whether to check if restarts are
                                      permitted before running hook.
        :type check_deferred_events: bool
        """
        changed = reactive.flags.is_flag_set(
            'config.changed.enable-auto-restarts')
        # Run method if enable-auto-restarts has changed, irrespective of what
        # it was changed to. This ensure that this method is not immediately
        # deferred when enable-auto-restarts is initially set to False.
        if ((not check_deferred_events) or changed or
                deferred_events.is_restart_permitted()):
            deferred_events.clear_deferred_hook('install')
            super().install()
        else:
            deferred_events.set_deferred_hook('install')


class OVNConfigurationAdapter(
        charms_openstack.adapters.ConfigurationAdapter):
    """Provide a configuration adapter for OVN."""

    class OSContextObjectView(object):

        def __init__(self, ctxt):
            """Initialize OSContextObjectView instance.

            :param ctxt: Dictionary with context variables
            :type ctxt: Dict[str,any]
            """
            self.__dict__ = ctxt

    def __init__(self, **kwargs):
        """Initialize contexts consumed from charm helpers."""
        super().__init__(**kwargs)
        self._dpdk_device = None
        self._sriov_device = None
        self._disable_mlockall = None
        if ch_core.hookenv.config('enable-dpdk'):
            self._dpdk_device = self.OSContextObjectView(
                os_context.DPDKDeviceContext(
                    bridges_key=self.charm_instance.bridges_key)())
        if (ch_core.hookenv.config('enable-hardware-offload') or
                ch_core.hookenv.config('enable-sriov')):
            self._sriov_device = os_context.SRIOVContext()

    @property
    def ovn_key(self):
        return os.path.join(self.charm_instance.ovn_sysconfdir(), 'key_host')

    @property
    def ovn_cert(self):
        return os.path.join(self.charm_instance.ovn_sysconfdir(), 'cert_host')

    @property
    def ovn_ca_cert(self):
        return os.path.join(self.charm_instance.ovn_sysconfdir(),
                            '{}.crt'.format(self.charm_instance.name))

    @property
    def dpdk_device(self):
        return self._dpdk_device

    @property
    def chassis_name(self):
        return self.charm_instance.get_ovs_hostname()

    @property
    def sriov_device(self):
        return self._sriov_device

    @property
    def mlockall_disabled(self):
        """Determine if Open vSwitch use of mlockall() should be disabled

        If the disable-mlockall config option is unset, mlockall will be
        disabled if running in a container and will default to enabled if
        not running in a container.
        """
        self._disable_mlockall = ch_core.hookenv.config('disable-mlockall')
        if self._disable_mlockall is None:
            self._disable_mlockall = False
            if ch_core.host.is_container():
                self._disable_mlockall = True
        return self._disable_mlockall


class NeutronPluginRelationAdapter(
        charms_openstack.adapters.OpenStackRelationAdapter):
    """Relation adapter for neutron-plugin interface."""

    @property
    def metadata_shared_secret(self):
        return self.relation.get_or_create_shared_secret()


class OVNChassisCharmRelationAdapters(
        charms_openstack.adapters.OpenStackRelationAdapters):
    """Provide dictionary of relation adapters for use by OVN Chassis charms.
    """
    relation_adapters = {
        # Note that RabbitMQ is only used for the Neutron SRIOV agent
        'amqp': charms_openstack.adapters.RabbitMQRelationAdapter,
        'nova_compute': NeutronPluginRelationAdapter,
    }


class BaseOVNChassisCharm(charms_openstack.charm.OpenStackCharm):
    """Base class for the OVN Chassis charms."""
    abstract_class = True
    package_codenames = {
        'ovn-host': collections.OrderedDict([
            ('2', 'train'),
            ('20', 'ussuri'),
        ]),
    }
    nova_vhost_user_file = '/etc/tmpfiles.d/nova-ovs-vhost-user.conf'
    release_pkg = 'ovn-host'
    adapters_class = OVNChassisCharmRelationAdapters
    configuration_class = OVNConfigurationAdapter
    required_relations = [CERT_RELATION, 'ovsdb']
    python_version = 3
    enable_openstack = False
    bridges_key = 'bridge-interface-mappings'
    valid_config = True
    # Services to be monitored by nrpe
    nrpe_check_base_services = []
    # Extra packages and services to be installed, managed and monitored if
    # charm forms part of an Openstack Deployment
    openstack_packages = []
    openstack_services = []
    openstack_restart_map = {}
    nrpe_check_openstack_services = []

    @property
    def nrpe_check_services(self):
        """Full list of services to be monitored by nrpe.

        :returns: List of services
        :rtype: List[str]
        """
        _check_services = self.nrpe_check_base_services[:]
        if self.enable_openstack:
            _check_services.extend(self.nrpe_check_openstack_services)
        return _check_services

    @property
    def enable_openstack(self):
        """Whether charm forms part of an OpenStack deployment.

        :returns: Whether charm forms part of an OpenStack deployment
        :rtype: boolean
        """
        return reactive.is_flag_set('charm.ovn-chassis.enable-openstack')

    @property
    def packages(self):
        """Full list of packages to be installed.

        :returns: List of packages
        :rtype: List[str]
        """
        _packages = ['ovn-host']
        if self.options.enable_dpdk:
            _packages.extend(['openvswitch-switch-dpdk'])
        if self.options.enable_hardware_offload or self.options.enable_sriov:
            # The ``sriov-netplan-shim`` package does boot-time
            # configuration of Virtual Functions (VFs) in the system.
            #
            # The charm does not do run-time configuration of VFs as this
            # would be detrimental to any instances consuming the VFs. In some
            # configurations it would also break NIC firmware LP: #1908351.
            #
            # NOTE: We consume the ``sriov-netplan-shim`` package both as a
            # charm wheel for the PCI Python library parts and as a deb for
            # the system init script and configuration tools.
            _packages.append('sriov-netplan-shim')
            if self.options.enable_hardware_offload:
                _packages.append('mlnx-switchdev-mode')
        if self.enable_openstack:
            if self.options.enable_sriov:
                _packages.append('neutron-sriov-agent')
            _packages.extend(self.openstack_packages)
        return _packages

    @property
    def group(self):
        """Group that should own files

        :returns: Group name
        :rtype: str
        """
        # When OpenStack support is enabled the various config files laid
        # out for Neutron agents need to have group ownership of 'neutron'
        # for the services to have access to them.
        if self.enable_openstack:
            return 'neutron'
        else:
            return 'root'

    @property
    def services(self):
        """Full list of services to be managed.

        :returns: List of services.
        :rtype: List[str]
        """
        _services = ['ovn-host']
        if self.enable_openstack:
            _services.extend(self.openstack_services)
        return _services

    @property
    def restart_map(self):
        """Which services should be notified when a file changes.

        :returns: Restart map
        :rtype: Dict[str, List[str]]
        """
        # Note that we use the standard config render features of
        # charms.openstack to just copy this file in place hence no
        # service attached.
        # The charm will configure the system-id at runtime in the
        # ``configure_ovs`` method.  The openvswitch-switch init script will
        # use the on-disk file on service restart.
        _restart_map = {
            '/etc/openvswitch/system-id.conf': [],
            '/etc/default/openvswitch-switch': [],
        }
        # The ``dpdk`` system init script takes care of binding devices
        # to the driver specified in configuration at run- and boot- time.
        #
        # NOTE: we must take care to perform device lookup and store
        # mapping information in the system before binding the interfaces
        # as important information such as hardware ethernet address (MAC)
        # will be harder to get at once bound. (The device disappears from
        # sysfs)
        if self.options.enable_dpdk:
            _restart_map.update({
                '/etc/dpdk/interfaces': ['dpdk']})
        if self.options.enable_hardware_offload or self.options.enable_sriov:
            _restart_map.update({
                '/etc/sriov-netplan-shim/interfaces.yaml': [],
            })
        if self.enable_openstack:
            _restart_map.update(self.openstack_restart_map)
            if self.options.enable_sriov:
                _restart_map.update({
                    '/etc/neutron/neutron.conf': ['neutron-sriov-agent'],
                    '/etc/neutron/plugins/ml2/sriov_agent.ini': [
                        'neutron-sriov-agent'],
                })
            elif self.options.enable_dpdk:
                # Note that we use the standard config render features of
                # charms.openstack to just copy this file in place hence no
                # service attached. systemd-tmpfiles-setup will take care of
                # it at boot and we will do a first-time initialization in the
                # ``install`` method.
                _restart_map.update({self.nova_vhost_user_file: []})
        return _restart_map

    def __init__(self, **kwargs):
        """Allow augmenting behaviour on external factors."""
        super().__init__(**kwargs)
        if (self.enable_openstack and self.options.enable_sriov
                and 'amqp' not in self.required_relations):
            self.required_relations.append('amqp')

    def install(self):
        """Extend the default install method to handle update-alternatives.
        """
        if self.options.enable_hardware_offload or self.options.enable_sriov:
            self.configure_source('networking-tools-source')

        super().install()

        if (not reactive.is_flag_set('charm.installed') and
                self.options.mlockall_disabled):
            # We need to render /etc/default/openvswitch-switch after the
            # initial install and restart openvswitch-switch. This is done to
            # ensure that when the disable-mlockall config option is unset,
            # mlockall is disabled when running in a container.
            # The ovn-host stop/start is needed until the following bug is
            # fixed: https://pad.lv/1913736. Really this is a work-around
            # to get the pause/resume test to work.
            self.render_configs(['/etc/default/openvswitch-switch'])
            ch_core.host.service_stop('ovn-host')
            ch_core.host.service_restart('openvswitch-switch')
            ch_core.host.service_start('ovn-host')

        if self.options.enable_dpdk:
            self.run('update-alternatives', '--set', 'ovs-vswitchd',
                     '/usr/lib/openvswitch-switch-dpdk/ovs-vswitchd-dpdk')
            # Do first-time initialization of the directory used for vhostuser
            # sockets.  Neutron is made aware of this path centrally by the
            # neutron-api-plugin-ovn charm.
            #
            # Neutron will make per chassis decisions based on chassis
            # configuration whether vif_type will be 'ovs' or 'vhostuser'.
            #
            # This allows having a mix of DPDK and non-DPDK nodes in the same
            # deployment.
            if self.enable_openstack and not os.path.exists(
                    '/run/libvirt-vhost-user'):
                self.render_configs([self.nova_vhost_user_file])
                self.run('systemd-tmpfiles', '--create')
        else:
            self.run('update-alternatives', '--set', 'ovs-vswitchd',
                     '/usr/lib/openvswitch-switch/ovs-vswitchd')

    def resume(self):
        """Do full hook execution on resume.

        A part of the migration strategy to OVN is to start the chassis charms
        paused only to resume them as soon as cleanup after the previous SDN
        has been completed.

        For this to work we need to run a full hook execution on resume to make
        sure the system is configured properly.
        """
        super().resume()

        ch_core.hookenv.log("Re-execing as full hook execution after resume.",
                            level=ch_core.hookenv.INFO)
        os.execl(
            '/usr/bin/env',
            'python3',
            os.path.join(ch_core.hookenv.charm_dir(), 'hooks/config-changed'),
        )

    def custom_assess_status_last_check(self):
        """Check if has valid bridge config and set to blocked if is invalid.

        Returns (None, None) if the interfaces are okay, or a status, message
        if the config is invalid.

        :returns status & message info
        :rtype: (status, message) or (None, None)
        """

        if not self.valid_config:
            message = ('Wrong format for bridge-interface-mappings. '
                       'Expected format is space-delimited list of '
                       'key-value pairs. Ex: "br-internet:00:00:5e:00:00:42 '
                       'br-provider:enp3s0f0"')
            return 'blocked', message

        return None, None

    def states_to_check(self, required_relations=None):
        """Override parent method to add custom messaging.

        Note that this method will only override the messaging for certain
        relations, any relations we don't know about will get the default
        treatment from the parent method.

        :param required_relations: Override `required_relations` class instance
                                   variable.
        :type required_relations: Optional[List[str]]
        :returns: Map of relation name to flags to check presence of
                  accompanied by status and message.
        :rtype: collections.OrderedDict[str, List[Tuple[str, str, str]]]
        """
        # Retrieve default state map
        states_to_check = super().states_to_check(
            required_relations=required_relations)

        # The parent method will always return a OrderedDict
        if CERT_RELATION in states_to_check:
            # for the certificates relation we want to replace all messaging
            states_to_check[CERT_RELATION] = [
                # the certificates relation has no connected state
                ('{}.available'.format(CERT_RELATION),
                 'blocked',
                 "'{}' missing".format(CERT_RELATION)),
                # we cannot proceed until Vault have provided server
                # certificates
                ('{}.server.certs.available'.format(CERT_RELATION),
                 'waiting',
                 "'{}' awaiting server certificate data"
                 .format(CERT_RELATION)),
            ]

        return states_to_check

    @staticmethod
    def ovn_sysconfdir():
        """Provide path to OVN system configuration."""
        return '/etc/ovn'

    def run(self, *args):
        """Run external process and return result.

        :param *args: Command name and arguments.
        :type *args: str
        :returns: Data about completed process
        :rtype: subprocess.CompletedProcess
        :raises: subprocess.CalledProcessError
        """
        cp = subprocess.run(
            args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=True,
            universal_newlines=True)
        ch_core.hookenv.log(cp, level=ch_core.hookenv.INFO)

    def get_certificate_requests(self):
        """Override default certificate request handler.

        We make use of OVN RBAC for authorization of writes from chassis nodes
        to the Southbound DB. The OVSDB server implementation makes use of the
        CN in the certificate to grant access to individual chassis. The
        chassis name and CN must match for this to work.
        """
        return {self.get_ovs_hostname(): {'sans': []}}

    def configure_tls(self, certificates_interface=None):
        """Override default handler prepare certs per OVNs taste.

        :param certificates_interface: A certificates relation
        :type certificates_interface: Optional[TlsRequires(reactive.Endpoint)]
        """
        # The default handler in ``OpenStackCharm`` class does the CA only
        tls_objects = self.get_certs_and_keys(
            certificates_interface=certificates_interface)
        if not tls_objects:
            # We have no configuration settings ssl_* nor a certificates
            # relation to vault.
            # Avoid LP Bug#1900457
            ch_core.hookenv.log(
                'No TLS objects available yet. Deferring TLS processing',
                level=ch_core.hookenv.DEBUG)
            return

        expected_cn = self.get_ovs_hostname()

        for tls_object in tls_objects:
            if tls_object.get('cn') != expected_cn:
                continue
            with open(self.options.ovn_ca_cert, 'w') as crt:
                chain = tls_object.get('chain')
                if chain:
                    crt.write(tls_object['ca'] + os.linesep + chain)
                else:
                    crt.write(tls_object['ca'])

            self.configure_cert(self.ovn_sysconfdir(),
                                tls_object['cert'],
                                tls_object['key'],
                                cn='host')
            break
        else:
            ch_core.hookenv.log('No certificate with CN matching hostname '
                                'configured in OVS: "{}"'
                                .format(expected_cn),
                                level=ch_core.hookenv.INFO)
            # Flag that we are not satisfied with the provided certificates
            reactive.clear_flag('certificates.available')

    @staticmethod
    def _format_addr(addr):
        """Validate and format IP address

        :param addr: IPv6 or IPv4 address
        :type addr: str
        :returns: Address string, optionally encapsulated in brackets ([])
        :rtype: str
        :raises: ValueError
        """
        ipaddr = ipaddress.ip_address(addr)
        if isinstance(ipaddr, ipaddress.IPv6Address):
            fmt = '[{}]'
        else:
            fmt = '{}'
        return fmt.format(ipaddr)

    def get_data_ip(self):
        """Get IP of interface bound to ``data`` binding.

        :returns: IP address
        :rtype: str
        """
        # juju will always return address information, regardless of actual
        # presence of space binding.
        #
        # Unpack ourself as ``network_get_primary_address`` is deprecated
        return self._format_addr(
            ch_core.hookenv.network_get(
                'data')['bind-addresses'][0]['addresses'][0]['address'])

    @staticmethod
    def get_ovs_hostname():
        """Get hostname (FQDN) from Open vSwitch.

        :returns: Hostname as configured in Open vSwitch
        :rtype: str
        :raises: KeyError
        """
        # The Open vSwitch ``ovs-ctl`` script has already done the dirty work
        # of retrieving the hosts FQDN, use it.
        #
        # In the unlikely event of the hostname not being set in the database
        # we want to error out as this will cause malfunction.
        for row in ch_ovsdb.SimpleOVSDB('ovs-vsctl').open_vswitch:
            return row['external_ids']['hostname']

    def configure_ovs_dpdk(self):
        """Configure DPDK specific bits in Open vSwitch.

        :returns: Whether something changed
        :rtype: bool
        """
        something_changed = False
        dpdk_context = os_context.OVSDPDKDeviceContext(
            bridges_key=self.bridges_key)
        opvs = ch_ovsdb.SimpleOVSDB('ovs-vsctl').open_vswitch
        other_config_fmt = 'other_config:{}'
        for row in opvs:
            for k, v in (('dpdk-lcore-mask', dpdk_context.cpu_mask()),
                         ('dpdk-socket-mem', dpdk_context.socket_memory()),
                         ('dpdk-init', 'true'),
                         ('dpdk-extra', dpdk_context.pci_whitelist()),
                         ):
                if row.get(other_config_fmt.format(k)) != v:
                    something_changed = True
                    if v:
                        opvs.set('.', other_config_fmt.format(k), v)
                    else:
                        opvs.remove('.', 'other_config', k)
        return something_changed

    def configure_ovs_hw_offload(self):
        """Configure hardware offload specific bits in Open vSwitch.

        :returns: Whether something changed
        :rtype: bool
        """
        something_changed = False
        opvs = ch_ovsdb.SimpleOVSDB('ovs-vsctl').open_vswitch
        other_config_fmt = 'other_config:{}'
        for row in opvs:
            for k, v in (('hw-offload', 'true'),
                         ('max-idle', '30000'),
                         ):
                if row.get(other_config_fmt.format(k)) != v:
                    something_changed = True
                    if v:
                        opvs.set('.', other_config_fmt.format(k), v)
                    else:
                        opvs.remove('.', 'other_config', k)
        return something_changed

    def configure_ovs(self, sb_conn, mlockall_changed):
        """Global Open vSwitch configuration tasks.

        :param sb_conn: Comma separated string of OVSDB connection methods.
        :type sb_conn: str

        Note that running this method will restart the ``openvswitch-switch``
        service if required.
        """
        if self.check_if_paused() != (None, None):
            ch_core.hookenv.log('Unit is paused, defer global Open vSwitch '
                                'configuration tasks.',
                                level=ch_core.hookenv.INFO)
            return

        if mlockall_changed:
            # NOTE(fnordahl): We need to act immediately to changes to
            # OVS_DEFAULT in-line. It is important to write config to disk
            # and perhaps restart the openvswitch-swith service prior to
            # attempting to do run-time configuration of OVS as we may have
            # to pass options to `ovs-vsctl` for `ovs-vswitchd` to run at all.
            ch_core.host.service_restart('openvswitch-switch')
        else:
            # Must make sure the service runs otherwise calls to ``ovs-vsctl``
            # will hang.
            ch_core.host.service_start('openvswitch-switch')

        restart_required = False
        # NOTE(fnordahl): Due to what is probably a bug in Open vSwitch
        # subsequent calls to ``ovs-vsctl set-ssl`` will hang indefinitely
        # Work around this by passing ``--no-wait``.
        self.run('ovs-vsctl',
                 '--no-wait',
                 'set-ssl',
                 self.options.ovn_key,
                 self.options.ovn_cert,
                 self.options.ovn_ca_cert)

        # The local ``ovn-controller`` process will retrieve information about
        # how to connect to OVN from the local Open vSwitch database.
        cmd = ('ovs-vsctl',)
        for ovs_ext_id in ('external-ids:ovn-encap-type=geneve',
                           'external-ids:ovn-encap-ip={}'
                           .format(self.get_data_ip()),
                           'external-ids:system-id={}'
                           .format(self.get_ovs_hostname()),
                           'external-ids:ovn-remote={}'.format(sb_conn),
                           'external_ids:ovn-match-northd-version=true',
                           ):
            cmd = cmd + ('--', 'set', 'open-vswitch', '.', ovs_ext_id)
        self.run(*cmd)
        if self.enable_openstack:
            # OpenStack Nova expects the local OVSDB server to listen to
            # TCP port 6640 on localhost.  We use this for the OVN metadata
            # agent too, as it allows us to run it as a non-root user.
            # LP: #1852200
            target = 'ptcp:6640:127.0.0.1'
            for el in ch_ovsdb.SimpleOVSDB(
                    'ovs-vsctl').manager.find('target="{}"'.format(target)):
                break
            else:
                self.run('ovs-vsctl', '--id', '@manager',
                         'create', 'Manager', 'target="{}"'.format(target),
                         '--', 'add', 'Open_vSwitch', '.', 'manager_options',
                         '@manager')

        if self.options.enable_hardware_offload:
            restart_required = self.configure_ovs_hw_offload()
        elif self.options.enable_dpdk:
            restart_required = self.configure_ovs_dpdk()
        if restart_required:
            ch_core.host.service_restart('openvswitch-switch')

    def configure_bridges(self):
        """Configure Open vSwitch bridges ports and interfaces."""
        if self.check_if_paused() != (None, None):
            ch_core.hookenv.log('Unit is paused, defer Open vSwitch bridge '
                                'port interface configuration tasks.',
                                level=ch_core.hookenv.INFO)
            return
        try:
            bpi = os_context.BridgePortInterfaceMap(
                bridges_key=self.bridges_key
            )
            self.valid_config = True

        except ValueError:
            self.valid_config = False
            return

        bond_config = os_context.BondConfig()
        ch_core.hookenv.log('BridgePortInterfaceMap: "{}"'.format(bpi.items()),
                            level=ch_core.hookenv.DEBUG)

        # build map of bridges to ovn networks with existing if-mapping on host
        # and at the same time build ovn-bridge-mappings string
        ovn_br_map_str = ''
        ovnbridges = collections.defaultdict(list)
        config_obm = self.config['ovn-bridge-mappings'] or ''
        for pair in sorted(config_obm.split()):
            network, bridge = pair.split(':', 1)
            if bridge in bpi:
                ovnbridges[bridge].append(network)
                if ovn_br_map_str:
                    ovn_br_map_str += ','
                ovn_br_map_str += '{}:{}'.format(network, bridge)

        bridges = ch_ovsdb.SimpleOVSDB('ovs-vsctl').bridge
        ports = ch_ovsdb.SimpleOVSDB('ovs-vsctl').port
        for bridge in bridges.find('external_ids:charm-ovn-chassis=managed'):
            # remove bridges and ports that are managed by us and no longer in
            # config
            if bridge['name'] not in bpi and bridge['name'] != 'br-int':
                ch_core.hookenv.log('removing bridge "{}" as it is no longer'
                                    'present in configuration for this unit.'
                                    .format(bridge['name']),
                                    level=ch_core.hookenv.DEBUG)
                ch_ovs.del_bridge(bridge['name'])
            else:
                for port in ports.find('external_ids:charm-ovn-chassis={}'
                                       .format(bridge['name'])):
                    if port['name'] not in bpi[bridge['name']]:
                        ch_core.hookenv.log('removing port "{}" from bridge '
                                            '"{}" as it is no longer present '
                                            'in configuration for this unit.'
                                            .format(port['name'],
                                                    bridge['name']),
                                            level=ch_core.hookenv.DEBUG)
                        ch_ovs.del_bridge_port(bridge['name'], port['name'])
        brdata = {
            'external-ids': {'charm-ovn-chassis': 'managed'},
            'protocols': 'OpenFlow13,OpenFlow15',
        }
        if self.options.enable_dpdk:
            brdata.update({'datapath-type': 'netdev'})
        else:
            brdata.update({'datapath-type': 'system'})
        # we always update the integration bridge to make sure it has settings
        # apropriate for the current charm configuration
        ch_ovs.add_bridge('br-int', brdata={
            **brdata,
            **{
                # for the integration bridge we want the datapath to await
                # controller action before adding any flows. This is to avoid
                # switching packets between isolated logical networks before
                # `ovn-controller` starts up.
                'fail-mode': 'secure',
                # Suppress in-band control flows for the integration bridge,
                # refer to ovn-architecture(7) for more details.
                'other-config': {'disable-in-band': 'true'},
            },
        })
        for br in bpi:
            if br not in ovnbridges:
                continue
            ch_ovs.add_bridge(br, brdata={
                **brdata,
                # for bridges used for external connectivity we want the
                # datapath to act like an ordinary MAC-learning switch.
                **{'fail-mode': 'standalone'},
            })
            for port in bpi[br]:
                ifdatamap = bpi.get_ifdatamap(br, port)
                ifdatamap = {
                    port: {
                        **ifdata,
                        **{'external-ids': {'charm-ovn-chassis': br}},
                    }
                    for port, ifdata in ifdatamap.items()
                }

                if len(ifdatamap) > 1:
                    ch_ovs.add_bridge_bond(br, port, list(ifdatamap.keys()),
                                           bond_config.get_ovs_portdata(port),
                                           ifdatamap)
                else:
                    ch_ovs.add_bridge_port(br, port,
                                           ifdata=ifdatamap.get(port, {}),
                                           linkup=not self.options.enable_dpdk,
                                           promisc=None,
                                           portdata={
                                               'external-ids': {
                                                   'charm-ovn-chassis': br}})

        opvs = ch_ovsdb.SimpleOVSDB('ovs-vsctl').open_vswitch
        if ovn_br_map_str:
            opvs.set('.', 'external_ids:ovn-bridge-mappings', ovn_br_map_str)
        else:
            opvs.remove('.', 'external_ids', 'ovn-bridge-mappings')

        if self.options.prefer_chassis_as_gw:
            opvs.set(
                '.', 'external_ids:ovn-cms-options', 'enable-chassis-as-gw')
        else:
            opvs.remove(
                '.', 'external_ids', 'ovn-cms-options=enable-chassis-as-gw')

    def render_nrpe(self):
        """Configure Nagios NRPE checks."""
        ch_core.hookenv.log("Rendering NRPE checks.",
                            level=ch_core.hookenv.INFO)
        hostname = nrpe.get_nagios_hostname()
        current_unit = nrpe.get_nagios_unit_name()
        # Determine if this is a subordinate unit or not
        if ch_core.hookenv.principal_unit() == ch_core.hookenv.local_unit():
            primary = True
        else:
            primary = False
        charm_nrpe = nrpe.NRPE(hostname=hostname, primary=primary)
        nrpe.add_init_service_checks(
            charm_nrpe, self.nrpe_check_services, current_unit)
        charm_nrpe.write()


class BaseTrainOVNChassisCharm(BaseOVNChassisCharm):
    """Train incarnation of the OVN Chassis base charm class."""
    abstract_class = True
    openstack_packages = ['networking-ovn-metadata-agent', 'haproxy']
    openstack_services = ['networking-ovn-metadata-agent']
    openstack_restart_map = {
        '/etc/neutron/networking_ovn_metadata_agent.ini': [
            'networking-ovn-metadata-agent']}
    nrpe_check_base_services = [
        'ovn-host',
        'ovs-vswitchd',
        'ovsdb-server']
    nrpe_check_openstack_services = [
        'networking-ovn-metadata-agent']

    @staticmethod
    def ovn_sysconfdir():
        return '/etc/openvswitch'


class BaseUssuriOVNChassisCharm(BaseOVNChassisCharm):
    """Ussuri incarnation of the OVN Chassis base charm class."""
    abstract_class = True
    openstack_packages = ['neutron-ovn-metadata-agent']
    openstack_services = ['neutron-ovn-metadata-agent']
    openstack_restart_map = {
        '/etc/neutron/neutron_ovn_metadata_agent.ini': [
            'neutron-ovn-metadata-agent']}
    nrpe_check_base_services = [
        'ovn-controller',
        'ovs-vswitchd',
        'ovsdb-server']
    nrpe_check_openstack_services = [
        'neutron-ovn-metadata-agent']
