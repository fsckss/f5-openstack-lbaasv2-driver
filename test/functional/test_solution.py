# Copyright 2106 F5 Networks Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#

import decorator
import paramiko
import pytest
import time
import sys

from f5.bigip import BigIP
from pprint import pprint as pp
from neutronclient.v2_0 import client
from f5_os_test.polling_clients import NeutronClientPollingManager
from f5_os_test.polling_clients import MaximumNumberOfAttemptsExceeded


def log_test_call(func):
    def wrapper(func, *args, **kwargs):
        print("\nRunning %s" % func.func_name)
        return func(*args, **kwargs)
    return decorator.decorator(wrapper, func)


class ExecTestEnv(object):
    lbaas_version = 2

    def __init__(self, symbols):
        self.symbols = symbols.copy()
        if 'lbaas_version' in self.symbols:
            lbaas_version =  self.symbols['lbaas_version']
        else:
            lbaas_version = ExecTestEnv.lbaas_version
        new_symbols = {
            'debug':            True,
            'lbaas_version':    lbaas_version,
            # leave the following settings alone
            'bigip_username':   'admin',
            'bigip_password':   'admin',
            # The provider string for v2 needs to change to match the
            # environment prefix in the f5 agent ini file
            'provider':         ('f5networks' if lbaas_version == 2 else
                                 'f5'),
            'admin_name':       'admin',
            'admin_username':   'admin',
            'admin_password':   'changeme',
            'tenant_name':      'testlab',
            'tenant_username':  'testlab',
            'tenant_password':  'changeme',
            'client_subnet':    'client-v4-subnet',
            'guest_username':   'centos',
            'guest_password':   'changeme',
            'server_http_port': '8080',
            'server_client_ip': '10.2.2.3'
        }
        self.symbols.update(new_symbols)


def nclientmanager(symbols):
    nclient_config = {
        'username': symbols['tenant_username'],
        'password': symbols['tenant_password'],
        'tenant_name': symbols['tenant_name'],
        'auth_url': symbols['openstack_auth_url']
    }

    neutronclient = client.Client(**nclient_config)
    #return NeutronClientPollingManager(neutronclient)
    return NeutronClientPollingManager(**nclient_config)


# candidate for moving to f5-openstack-test as common utility to create a
# full proxy capable of sending traffic
class LBaaSv1(object):
    def __init__(self, symbols):
        self.polling_interval = 1
        self.symbols = symbols
        self.ncm = nclientmanager(self.symbols)
        self.proxies = []
        self.max_attempts = 60

    def create_pool(self):
        pool_conf = {}
        for sn in self.ncm.list_subnets()['subnets']:
            if sn['name'] == self.symbols['client_subnet']:
                pool_conf = {'pool': {'tenant_id':  sn['tenant_id'],
                                      'lb_method':  'ROUND_ROBIN',
                                      'protocol':   'HTTP',
                                      'subnet_id':  sn['id'],
                                      'provider':   self.symbols['provider'],
                                      'name':       'test_pool_01'}}

        return self.ncm.create_pool(pool_conf)['pool']

    def wait_for_object_state(self, field, value, method, key, *args):
        '''
        This method provides an abstract way to poll any arbitrary object, such
        as pool, member, vip, etc.

        :param field: the attribute in the object that you want to monitor
        :param value: the final value that you want to see
        :param method: the show method that returns the object containing state
        :param key: the key to a aub-dict in the show method output that
                    contains the state
        :param args: necessary args that must be passed to the show method
        :return:  N/A, raise if 'value' is not seen within 60 seconds
        '''

        time.sleep(self.polling_interval)
        current_state = method(*args)[key]
        current_value = current_state[field]
        attempts = 0
        while value != current_value:
            sys.stdout.flush()
            current_state = method(*args)[key]
            current_value = current_state[field]
            time.sleep(self.polling_interval)
            attempts = attempts + 1
            if attempts >= self.max_attempts:
                raise MaximumNumberOfAttemptsExceeded

    def create_vip(self, pool_id, subnet_id):
        vip_conf = {'vip': {'name': 'test_vip_01',
                            'pool_id': pool_id,
                            'subnet_id': subnet_id,
                            'protocol': 'HTTP',
                            'protocol_port': 80}}
        return self.ncm.create_vip(vip_conf)['vip']

    def create_member(self, pool_id):
        member_conf = {
            'member': {'pool_id': pool_id,
                       'address': self.symbols['server_client_ip'],
                       'protocol_port': int(self.symbols['server_http_port'])}}
        return self.ncm.create_member(member_conf)['member']

    def create_healthmonitor(self):
        hm_conf = {'health_monitor': {'type': 'HTTP',
                                      'delay': 5,
                                      'timeout': 3,
                                      'max_retries': 2}}
        return self.ncm.create_health_monitor(hm_conf)['health_monitor']

    def create_proxy(self):
        class Proxy(object):
            def __init__(self):
                self.pool = None
                self.vip = None
                self.members = []
                self.healthmonitors = []

        # create a fully-formed loadbalancer with VIP, members, etc. suitable
        # for passing traffic between client and server
        proxy = Proxy()
        proxy.pool = self.create_pool()
        proxy.vip = self.create_vip(proxy.pool['id'], proxy.pool['subnet_id'])
        proxy.members.append(self.create_member(proxy.pool['id']))
        proxy.healthmonitors.append(self.create_healthmonitor())
        hmconf = proxy.healthmonitors[0]
        hmconf = {'health_monitor': {'id': hmconf['id'],
                                     'tenant_id': hmconf['tenant_id']}}
        self.ncm.client.associate_health_monitor(proxy.pool['id'], hmconf)
        self.proxies.append(proxy)
        return proxy

    def delete_proxy(self, proxy):
        # destroy a proxy and all associated objects
        for healthmonitor in proxy.healthmonitors:
            self.ncm.client.disassociate_health_monitor(proxy.pool['id'],
                                                        proxy.healthmonitors[0]['id'])
            self.ncm.delete_health_monitor(healthmonitor['id'])
        for member in proxy.members:
            self.ncm.delete_member(member['id'])
        self.ncm.delete_vip(proxy.vip['id'])
        self.wait_for_object_state('vip_id', None,
                                   self.ncm.show_pool, 'pool', proxy.pool['id'])
        self.ncm.delete_pool(proxy.pool['id'])
        # workaround for bug? need to explicitly delete selfip and route domain
        # for the pool to be deleted, otherwise it is stuck in pending.

    def clear_proxies(self):
        for vip in self.ncm.list_vips()['vips']:
            self.ncm.delete_vip(vip['id'])
        for member in self.ncm.list_members()['members']:
            self.ncm.delete_member(member['id'])
        for pool in self.ncm.list_pools()['pools']:
            pp(pool)
            for health_monitor in \
                    self.ncm.list_health_monitors()['health_monitors']:
                self.ncm.client.disassociate_health_monitor(pool['id'],
                                                            health_monitor['id'])
                time.sleep(5)
                self.ncm.delete_health_monitor(health_monitor['id'])
            self.wait_for_object_state('vip_id', None,
                                       self.ncm.show_pool, 'pool', pool['id'])
            self.ncm.delete_pool(pool['id'])
        if self.symbols['debug']:
            self.debug()

    def debug(self):
        print '----- pools -----'
        pp(self.ncm.list_pools())
        print '----- vips -----'
        pp(self.ncm.list_vips())
        print '----- members -----'
        pp(self.ncm.list_members())
        print '----- health monitors -----'
        pp(self.ncm.list_health_monitors())


# candidate for moving to f5-openstack-test as common utility to create a
# full proxy capable of sending traffic
class LBaaSv2(object):
    def __init__(self, symbols):
        self.polling_interval = 1
        self.symbols = symbols
        self.ncm = nclientmanager(self.symbols)
        self.proxies = []

    def create_loadbalancer(self):
        lb_conf = {}
        for sn in self.ncm.list_subnets()['subnets']:
            if sn['name'] == self.symbols['client_subnet']:
                lb_conf = {
                    'loadbalancer': {
                        'vip_subnet_id': sn['id'],
                        #'lb_method':     'ROUND_ROBIN',
                        #'protocol':      'HTTP',
                        'tenant_id':     sn['tenant_id'],
                        'provider':      self.symbols['provider'],
                        'name':          'testlb_01'}}
        return self.ncm.create_loadbalancer(lb_conf)['loadbalancer']

    def delete_loadbalancer(self, lb):
        self.ncm.delete_loadbalancer(lb['id'])

    def wait_for_object_state(self, field, value, method, key, *args):
        '''
        This method provides an abstract way to poll any arbitrary object, such
        as pool, member, vip, etc.

        :param field: the attribute in the object that you want to monitor
        :param value: the final value that you want to see
        :param method: the show method that returns the object containing state
        :param key: the key to a aub-dict in the show method output that
                    contains the state
        :param args: necessary args that must be passed to the show method
        :return:  N/A, raise if 'value' is not seen within 60 seconds
        '''

        time.sleep(self.polling_interval)
        current_state = method(*args)[key]
        current_value = current_state[field]
        attempts = 0
        while value != current_value:
            sys.stdout.flush()
            current_state = method(*args)[key]
            pp(current_state)
            current_value = current_state[field]
            time.sleep(self.polling_interval)
            attempts = attempts + 1
            if attempts >= self.max_attempts:
                raise MaximumNumberOfAttemptsExceeded

    def create_listener(self, lb_id):
        listener_config =\
        {'listener': {'name': 'test_listener',
                      'loadbalancer_id': lb_id,
                      'protocol': 'HTTP',
                      'protocol_port': 80}}
        return self.ncm.create_listener(listener_config)['listener']

    def create_lbaas_pool(self, l_id):
        pool_config = {
            'pool': {
                'name': 'test_pool_anur23rgg',
                'lb_algorithm': 'ROUND_ROBIN',
                'listener_id': l_id,
                'protocol': 'HTTP'}}
        return self.ncm.create_lbaas_pool(pool_config)['pool']

    def create_lbaas_member(self, p_id):
        subnet_id = None
        for sn in self.ncm.list_subnets()['subnets']:
            if 'server-v4' in sn['name']:
                subnet_id = sn['id']
                break

        member_config = {
            'member': {
                'subnet_id': subnet_id,
                'address': self.symbols['server_client_ip'],
                'protocol_port': int(self.symbols['server_http_port'])}}

        return self.ncm.create_lbaas_member(p_id, member_config)['member']

    def create_lbaas_healthmonitor(self, p_id):
        hm_config = {
            'healthmonitor': {
                'type': 'HTTP',
                'delay': 5,
                'timeout': 3,
                'pool_id': p_id,
                'max_retries': 2}}
        # direct call to client, missing polling layer
        return self.ncm.create_lbaas_healthmonitor(hm_config)['healthmonitor']

    def create_proxy(self):
        class Proxy(object):
            def __init__(self):
                self.loadbalancer = None
                self.listener = None
                self.lbaas_pool = None
                self.members = []
                self.healthmonitors = []

        # create a fully-formed loadbalancer with VIP, members, etc. suitable
        # for passing traffic between client and server
        proxy = Proxy()
        proxy.loadbalancer = self.create_loadbalancer()
        proxy.listener = self.create_listener(proxy.loadbalancer['id'])
        proxy.pool = self.create_lbaas_pool(proxy.listener['id'])
        proxy.members.append(self.create_lbaas_member(proxy.pool['id']))
        # lbaasv2 bug: lbaas_show_member does not return status attribute to
        # know when member comes online.
        proxy.healthmonitors.append(
            self.create_lbaas_healthmonitor(proxy.pool['id']))
        self.proxies.append(proxy)
        return proxy

    def delete_proxy(self, proxy):
        # destroy a loadbalancer and all associated objects
        for healthmonitor in proxy.healthmonitors:
            self.ncm.delete_lbaas_healthmonitor(healthmonitor['id'])
        for member in proxy.members:
            self.ncm.delete_lbaas_member(member['id'])
        self.ncm.delete_lbaas_pool(proxy.pool['id'])
        self.ncm.delete_listener(proxy.listener['id'])
        self.ncm.delete_loadbalancer(proxy.loadbalancer['id'])

    def clear_proxies(self):
        self.ncm.delete_all_lbaas_healthmonitors()
        self.ncm.delete_all_lbaas_pools()
        self.ncm.delete_all_listeners()
        self.ncm.delete_all_loadbalancers()
        if self.symbols['debug']:
            self.debug()

    def debug(self):
        print '----- loadbalancers -----'
        pp(self.ncm.list_loadbalancers())
        print '----- listeners -----'
        pp(self.ncm.list_listeners())
        print '----- pools -----'
        pp(self.ncm.list_lbaas_pools())
        print '----- members -----'
        for p in self.ncm.list_lbaas_pools()['pools']:
            pp(self.ncm.list_lbaas_members(p['id']))
        print '----- health monitors -----'
        pp(self.ncm.list_lbaas_healthmonitors())


@pytest.fixture
def tst_setup(request, symbols):
    print 'test setup'
    # change the following settings to use your TLC session
    # temp solution until we integrate with Kevin's py-symbols module.
    symbols_env = {
        'bigip_public_mgmt_ip':     symbols.bigip_ip,
        'client_public_mgmt_ip':    symbols.client_ip,
        'server_public_mgmt_ip':    symbols.server_ip,
        'openstack_auth_url':       symbols.auth_url
    }
    testenv = ExecTestEnv(symbols_env)

    def tst_teardown():
        print 'test teardown'
        # pool delete sometimes get stuck in pending due to one or more of the
        # following objects existing on BIG-IP:
        # selfip, snat, route-domain, vxlan tunnel, gre tunnel
        testenv.lbm.clear_proxies()
        if testenv.webserver_started:
            exec_command(testenv.server_ssh, 'pkill -f SimpleHTTPServer')
        testenv.client_ssh.close()
        testenv.server_ssh.close()

    # create SSH conn to client
    testenv.client_ssh = paramiko.SSHClient()
    testenv.client_ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    testenv.client_ssh.connect(testenv.symbols['client_public_mgmt_ip'],
                               username=testenv.symbols['guest_username'],
                               password=testenv.symbols['guest_password'])
    # create SSH conn to server
    testenv.server_ssh = paramiko.SSHClient()
    testenv.server_ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    testenv.server_ssh.connect(testenv.symbols['server_public_mgmt_ip'],
                               username=testenv.symbols['guest_username'],
                               password=testenv.symbols['guest_password'])
    #
    testenv.bigip = BigIP(testenv.symbols['bigip_public_mgmt_ip'],
                          testenv.symbols['bigip_username'],
                          testenv.symbols['bigip_password'])
    testenv.request = request
    testenv.lbm = (LBaaSv1(testenv.symbols) if
                   testenv.symbols['lbaas_version'] == 1 else
                   LBaaSv2(testenv.symbols))
    testenv.lbm.clear_proxies()
    #request.addfinalizer(tst_teardown)
    return testenv


def exec_command(ssh, command):
    stdin, stdout, stderr = ssh.exec_command(command)
    stdin.close()
    status = stdout.channel.recv_exit_status()
    output = stdout.read()
    if status:
        print '----- sterr -----'
        print stderr
        pp(stderr.channel.__dict__)
        error = stderr.read()
        print error
    assert status == 0
    return output.strip()


@log_test_call
def test_solution(tst_setup):
    te = tst_setup
    te.webserver_started = False
    proxy = te.lbm.create_proxy()

    # start web server
    command = ('python -m SimpleHTTPServer %s >/dev/null 2>&1 & echo $!' %
               te.symbols['server_http_port'])
    exec_command(te.server_ssh, command)
    te.webserver_started = True

    # wait for health monitor to show server as up
    print 'waiting for member to become active...',
    if te.symbols['lbaas_version'] == 1:
        te.lbm.wait_for_object_state('status', 'ACTIVE',
                                     te.lbm.ncm.show_member, 'member',
                                     proxy.members[0]['id'])
    else:
        # lbaasv2 bug: lbaas_show_member does not return status attribute to
        # know when member comes online.
        #te.lbm.wait_for_object_state('status', 'ACTIVE',
        #                             te.lbm.ncm.show_lbaas_member, 'member',
        #                             proxy.members[0]['id'], proxy.pool['id'])

        # HACK workaround until openstack supports the status field
        folders = te.bigip.sys.folders.get_collection()
        for f in folders:
            if f.name.startswith('Project_'):
                break
        params = {'params': {'$filter': 'partition eq %s' % f.name}}
        pool = te.bigip.ltm.pools.pool.load(name=proxy.pool['name'],
                                            partition=f.name)
        members = pool.members_s.get_collection(request_params=params)
        found = False
        for member in members:
            if member.address.split('%')[0] == proxy.members[0]['address']:
                found = True
                break
        assert(found)
        attempts = 0
        while member.state != 'up':
            time.sleep(1)
            attempts = attempts + 1
            if attempts >= 15:
                raise MaximumNumberOfAttemptsExceeded
            member.refresh()
    print 'COMPLETE'

    # send requests from client
    print 'sending request from client....',
    if te.symbols['lbaas_version'] == 1:
        url = 'http://%s:%s' % (proxy.vip['address'],
                                proxy.vip['protocol_port'])
    else:
        url = 'http://%s:%s' % (proxy.loadbalancer['vip_address'],
                                proxy.listener['protocol_port'])
    output = exec_command(te.client_ssh, '$HOME/get.py %s' % url)
    assert output == '200'
    print 'SUCCESS'
