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

import collections
import os

from kuryr.lib._i18n import _
from oslo_concurrency import lockutils
from oslo_concurrency import processutils
from oslo_config import cfg
from oslo_log import log as logging

from kuryr_kubernetes import clients
from kuryr_kubernetes.cni.binding import base as b_base
from kuryr_kubernetes import config
from kuryr_kubernetes import constants
from kuryr_kubernetes import exceptions
from kuryr_kubernetes import utils

LOG = logging.getLogger(__name__)
CONF = cfg.CONF


class VIFSriovDriver(object):
    def __init__(self):
        self._lock = None
        self._device_pf_mapping = self._get_device_pf_mapping()

    def release_lock_object(func):
        def wrapped(self, *args, **kwargs):
            try:
                return func(self, *args, **kwargs)
            finally:
                if self._lock and self._lock.acquired:
                    self._lock.release()
        return wrapped

    @release_lock_object
    def connect(self, vif, ifname, netns, container_id):
        physnet = vif.physnet
        pf_names = self._get_host_pf_names(physnet)
        vf_name, vf_index, pf, pci_info = self._get_available_vf_info(pf_names)

        if not vf_name:
            raise exceptions.CNIError(
                "No free interfaces for physnet {} available".format(physnet))

        LOG.debug("Connect {} as {} (port_id={}) in container_id={}".format(
            vf_name, ifname, vif.id, container_id))

        if vif.network.should_provide_vlan:
            vlan_id = vif.network.vlan
            self._set_vf_vlan(pf, vf_index, vlan_id)

        self._set_vf_mac(pf, vf_index, vif.address)

        with b_base.get_ipdb() as h_ipdb, b_base.get_ipdb(netns) as c_ipdb:
            with h_ipdb.interfaces[vf_name] as host_iface:
                host_iface.net_ns_fd = utils.convert_netns(netns)

            with c_ipdb.interfaces[vf_name] as iface:
                iface.ifname = ifname
                iface.mtu = vif.network.mtu
                iface.up()

        self._save_pci_info(vif.id, pci_info)

    def disconnect(self, vif, ifname, netns, container_id):
        # NOTE(k.zaitsev): when netns is deleted the interface is
        # returned automatically to host netns. We may reset
        # it to all-zero state
        self._remove_pci_info(vif.id)

    def _get_host_pf_names(self, physnet):
        """Return a list of PFs, that belong to a physnet"""

        if physnet not in self._device_pf_mapping:
            raise cfg.Error(
                "No mapping for physnet {} in {}".format(
                    physnet, self._device_pf_mapping))
        return self._device_pf_mapping[physnet]

    def _get_available_vf_info(self, pf_names):
        """Scan /sys for unacquired VF among PFs in pf_names"""

        for pf in pf_names:
            pf_sys_path = '/sys/class/net/{}/device'.format(pf)
            nvfs = self._get_total_vfs(pf)
            for vf_index in range(nvfs):
                vf_sys_path = os.path.join(pf_sys_path,
                                           'virtfn{}'.format(vf_index),
                                           'net')
                # TODO(kzaitsev): use /var/run/kuryr/smth
                lock_path = os.path.join("/tmp",
                                         "{}.{}".format(pf, vf_index))
                self._acquire(lock_path)
                LOG.debug("Aquired %s lock", lock_path)
                try:
                    vf_names = os.listdir(vf_sys_path)
                except OSError:
                    LOG.debug("Could not open %s. "
                              "Skipping vf %s for pf %s", vf_sys_path,
                              vf_index, pf)
                    self._release()
                    continue
                if not vf_names:
                    LOG.debug("No interfaces in %s. "
                              "Skipping vf %s for pf %s", vf_sys_path,
                              vf_index, pf)
                    self._release()
                    continue
                vf_name = vf_names[0]
                pci_info = self._get_pci_info(pf, vf_index)
                LOG.debug("Aquiring vf %s of pf %s", vf_index, pf)
                return vf_name, vf_index, pf, pci_info
        return None, None, None, None

    def _get_pci_info(self, pf, vf_index):
        pci_slot = ''
        physnet = ''
        pci_vendor_info = ''

        vendor_path = '/sys/class/net/{}/device/virtfn{}/vendor'.format(
            pf, vf_index)
        with open(vendor_path) as vendor_file:
            vendor_full = vendor_file.read()
            vendor = vendor_full.split('x')[1].strip()
        device_path = '/sys/class/net/{}/device/virtfn{}/device'.format(
            pf, vf_index)
        with open(device_path) as device_file:
            device_full = device_file.read()
            device = device_full.split('x')[1].strip()
        pci_vendor_info = '{}:{}'.format(vendor, device)

        vf_path = '/sys/class/net/{}/device/virtfn{}'.format(
            pf, vf_index)
        pci_slot_path = os.readlink(vf_path)
        pci_slot = pci_slot_path.split('/')[1]

        physnet = self._get_physnet_by_pf(pf)

        return {'pci_slot': pci_slot,
                'physical_network': physnet,
                'pci_vendor_info': pci_vendor_info}

    def _get_physnet_by_pf(self, desired_pf):
        for physnet, pf_list in self._device_pf_mapping.items():
            for pf in pf_list:
                if pf == desired_pf:
                    return physnet
        LOG.exception("Unable to find physnet for pf %s", desired_pf)
        raise

    def _save_pci_info(self, neutron_port, port_pci_info):
        k8s = clients.get_kubernetes_client()
        annot_name = constants.K8S_ANNOTATION_NODE_PCI_DEVICE_INFO
        annot_name = annot_name.replace('/', '~1')
        annot_name = annot_name + '-' + neutron_port
        LOG.info('annot_name = %s', annot_name)
        nodename = utils.get_node_name()

        LOG.info("Trying to annotate node %s with pci info %s",
                 nodename, port_pci_info)
        k8s.patch_node_annotations(nodename, annot_name, port_pci_info)

    def _remove_pci_info(self, neutron_port):
        k8s = clients.get_kubernetes_client()
        annot_name = constants.K8S_ANNOTATION_NODE_PCI_DEVICE_INFO
        annot_name = annot_name.replace('/', '~1')
        annot_name = annot_name + '-' + neutron_port
        LOG.info('annot_name = %s', annot_name)
        nodename = utils.get_node_name()

        LOG.info("Trying to delete pci info for port %s on node %s",
                 neutron_port, nodename)
        k8s.remove_node_annotations(nodename, annot_name)

    def _acquire(self, path):
        if self._lock and self._lock.acquired:
            raise RuntimeError(_("Attempting to lock {} when {} "
                               "is already locked.").format(path, self._lock))
        self._lock = lockutils.InterProcessLock(path=path)
        return self._lock.acquire()

    def _release(self):
        if not self._lock:
            raise RuntimeError(_("Attempting release an empty lock"))
        return self._lock.release()

    def _get_total_vfs(self, pf):
        """Read /sys information for configured number of VFs of a PF"""

        pf_sys_path = '/sys/class/net/{}/device'.format(pf)
        total_fname = os.path.join(pf_sys_path, 'sriov_numvfs')
        try:
            with open(total_fname) as total_f:
                data = total_f.read()
        except IOError:
            LOG.warning("Could not open %s. No VFs for %s", total_fname, pf)
            return 0
        nvfs = 0
        try:
            nvfs = int(data.strip())
        except ValueError:
            LOG.warning("Could not parse %s from %s. No VFs for %s", data,
                        total_fname, pf)
            return 0
        LOG.debug("PF %s has %s VFs", pf, nvfs)
        return nvfs

    def _set_vf_mac(self, pf, vf_index, mac):
        """Call `ip link set enp2s0f1 vf 3 mac fa:16:3e:87:b2:ac`"""

        LOG.debug("Seting VF MAC: pf = %s, vf_index = %s, mac = %s",
                  pf, vf_index, mac)
        cmd = [
            'ip', 'link',
            'set', pf, 'vf', vf_index, 'mac', mac
        ]
        try:
            return processutils.execute(*cmd, run_as_root=True)
        except Exception:
            LOG.exception("Unable to execute %s", cmd)
            raise

    def _set_vf_vlan(self, pf, vf_index, vlan_id):
        """Call `ip link set enp1s0f0 vf 5 vlan 10`"""
        cmd = [
            'ip', 'link',
            'set', pf, 'vf', vf_index, 'vlan', vlan_id
        ]
        try:
            return processutils.execute(*cmd, run_as_root=True)
        except Exception:
            LOG.exception("Unable to execute %s", cmd)
            raise

    def is_alive(self):
        bridge_name = CONF.neutron_defaults.ovs_bridge
        try:
            with b_base.get_ipdb() as h_ipdb:
                h_ipdb.interfaces[bridge_name]
            return True
        except Exception:
            LOG.warning("Default OVS bridge %s does not exist on "
                        "the host.", bridge_name)
            return False

    def _get_device_pf_mapping(self):
        """Return a mapping in format {<physnet_name>:[<pf_name>, ...]}"""

        phys_mappings = config.CONF.sriov.physical_device_mappings
        physnets = collections.defaultdict(list)
        for phys_map in phys_mappings:
            try:
                netname, ifname = phys_map.split(':', 1)
            except ValueError:
                raise cfg.Error(
                    "Invalid mapping {}".format(phys_map))
            physnets[netname].append(ifname)
        return physnets
