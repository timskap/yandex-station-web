"""Discovery framing, bounded network scope and per-device opening."""
import json
import socketserver
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock
import discovery
import multi


class ProbeTests(unittest.TestCase):
    def probe_server(self, mode):
        opened=[]
        class Peer(socketserver.BaseRequestHandler):
            def handle(self):
                try:
                    cmd,_,_,_=discovery.receive(self.request,time.monotonic()+2)
                    if cmd!=b'CNXN':return
                    if mode=='auth':
                        self.request.sendall(discovery.packet(b'AUTH',1,0,b'challenge'));return
                    self.request.sendall(discovery.packet(b'CNXN',0x01000000,4096,b'device::\0'))
                    cmd,local,_,payload=discovery.receive(self.request,time.monotonic()+2)
                    opened.append(payload)
                    self.request.sendall(discovery.packet(b'OKAY',12,local))
                    data=b'\nPANEL_STATION\nyandexmicro\n00:11:22:33:44:55\nstation\n' if mode=='speaker' else b'ordinary Android device\n'
                    frame=discovery.packet(b'WRTE',12,local,data)
                    # Real TCP reads need not align with ADB messages.
                    for i in range(0,len(frame),7):self.request.sendall(frame[i:i+7])
                    discovery.receive(self.request,time.monotonic()+2)
                    self.request.sendall(discovery.packet(b'CLSE',12,local))
                except OSError:pass
        with socketserver.TCPServer(('127.0.0.1',0),Peer) as srv:
            thread=threading.Thread(target=srv.handle_request);thread.start()
            result=discovery.probe('127.0.0.1',srv.server_address[1]);thread.join(3)
        return result,opened

    def test_only_confirmed_speakers_are_listed(self):
        result,opened=self.probe_server('speaker')
        self.assertEqual(result['platform'],'yandexmicro')
        self.assertEqual(result['name'],'Станция Лайт')
        self.assertEqual(opened,[('shell:'+discovery.IDENTIFY+'\0').encode()])
        result,_=self.probe_server('other')
        self.assertIsNone(result)

    def test_authentication_is_not_bypassed_or_prompted(self):
        result,opened=self.probe_server('auth')
        self.assertIsNone(result);self.assertEqual(opened,[])


class ScannerTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.scanner=discovery.Scanner(Path(self.tmp.name)/'settings.json',known=['192.168.4.20:5555'])

    def test_scope_and_port_limits(self):
        for ranges in (['0.0.0.0/0'],['8.8.8.0/24'],['127.0.0.0/8'],['10.0.0.0/16'],['::1/128']):
            with self.subTest(ranges=ranges),self.assertRaises(ValueError):discovery.validate_networks(ranges)
        for ports in ([0],[65536],[True],['5555'],[1,2,3,4,5]):
            with self.subTest(ports=ports),self.assertRaises(ValueError):discovery.validate_ports(ports)
        self.assertEqual(discovery.validate_ports([5555,5555,2222]),[5555,2222])
        self.scanner.configure({'networks':['192.168.1.10/32'],'ports':[2222],'auto':False})
        self.assertEqual(self.scanner.targets(),(['192.168.1.10/32'],[('192.168.1.10',2222)]))
        loaded=discovery.Scanner(self.scanner.path)
        self.assertFalse(loaded.auto);self.assertEqual(loaded.ports,[2222])

    def test_detect_interfaces_and_remembered_network(self):
        output='en0: flags=8863<UP,BROADCAST,RUNNING>\n\tinet 192.168.0.16 netmask 0xffffff00\nutun0: flags=8051<UP>\n\tinet 10.0.0.2 netmask 0xffffff00\n'
        with mock.patch.object(discovery.sys,'platform','darwin'),mock.patch.object(discovery,'run',return_value=output):
            self.assertEqual(discovery.interface_networks(),['192.168.0.0/24'])
            networks,targets=self.scanner.targets()
        self.assertIn('192.168.4.0/24',networks)
        self.assertIn(('192.168.4.20',5555),targets)
        self.assertNotIn(('10.0.0.2',5555),targets)

    def test_finished_scan_marks_disappeared_device_offline(self):
        self.scanner.configure({'networks':['192.168.1.10/32'],'ports':[5555]})
        device={'id':'test','address':'192.168.1.10:5555','available':True,'last_seen':time.time()}
        with mock.patch.object(discovery,'probe',return_value=device):self.scanner._scan()
        self.assertTrue(self.scanner.resolve('test')['available'])
        with mock.patch.object(discovery,'probe',return_value=None):self.scanner._scan()
        self.assertFalse(self.scanner.snapshot()['devices'][0]['available'])
        with self.assertRaises(ValueError):self.scanner.resolve('test')

    def test_overlapping_scans_do_not_start_another_sweep(self):
        entered,release=threading.Event(),threading.Event()
        self.scanner.configure({'networks':['192.168.1.10/32'],'ports':[5555]})
        def wait(*args):entered.set();release.wait(2)
        with mock.patch.object(discovery,'probe',side_effect=wait) as probe:
            self.scanner.start();self.assertTrue(entered.wait(2));self.scanner.start();release.set()
            deadline=time.monotonic()+2
            while self.scanner.snapshot()['scanning'] and time.monotonic()<deadline:time.sleep(.01)
            self.assertEqual(probe.call_count,1)


class PortalTests(unittest.TestCase):
    def setUp(self):
        self.old=dict(multi.DEVICES);multi.DEVICES.clear()
        self.addCleanup(lambda:(multi.DEVICES.clear(),multi.DEVICES.update(self.old)))
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        multi.DEVICES['current']={'name':'Current','model':'lite2','device':'192.168.1.10:5555','port':8765}
        self.workers=multi.Workers({},self.root/'data/discovery')
        with mock.patch.object(discovery.Scanner,'run'):
            self.portal=multi.DiscoveryPortal(self.root,self.workers,root_id='current')
        self.addCleanup(self.portal.close)

    def add_found(self,identifier,address):
        self.portal.scanner.devices[identifier]={'id':identifier,'name':'Станция','model':'lite2','address':address,'available':True,'last_seen':time.time()}

    def test_current_device_reuses_existing_panel(self):
        multi.DEVICES['current']['device']='192.168.1.10'
        self.add_found('found','192.168.1.10:5555')
        with mock.patch.object(self.workers,'start_one') as start:
            result=self.portal.request('/api/discovery/open','POST',{'id':'found'})
            self.assertEqual(result['url'],'/speaker/current/');start.assert_not_called()

    def test_new_device_gets_pinned_worker_and_persistent_entry(self):
        self.add_found('s-abc','192.168.1.20:5555')
        with mock.patch.object(self.workers,'start_one') as start:
            result=self.portal.request('/api/discovery/open','POST',{'id':'s-abc'})
            self.assertEqual(result['url'],'/speaker/s-abc/')
            self.assertEqual(start.call_args.args[1]['device'],'192.168.1.20:5555')
        self.assertEqual(multi.DEVICES['current']['device'],'192.168.1.10:5555')
        saved=json.loads(self.portal.file.read_text())
        self.assertEqual(saved['speakers'][0]['id'],'s-abc')
        self.assertEqual(self.portal.file.stat().st_mode & 0o777,0o600)

    def test_saved_devices_survive_restart_without_starting_workers(self):
        self.add_found('s-abc','192.168.1.20:5555')
        with mock.patch.object(self.workers,'start_one'):
            self.portal.request('/api/discovery/open','POST',{'id':'s-abc'})
        multi.DEVICES.pop('s-abc')
        workers=multi.Workers({},self.root/'data/discovery')
        with mock.patch.object(discovery.Scanner,'run'),mock.patch.object(workers,'start_one') as start:
            reloaded=multi.DiscoveryPortal(self.root,workers,root_id='current')
            self.assertEqual(multi.DEVICES['s-abc']['device'],'192.168.1.20:5555')
            start.assert_not_called();reloaded.close()

    def test_changed_address_keeps_the_same_device_scope(self):
        multi.DEVICES['s-abc']={'name':'Speaker','model':'lite2','device':'192.168.1.20:5555'}
        self.add_found('s-abc','192.168.1.30:5555')
        with mock.patch.object(self.workers,'start_one') as start,mock.patch.object(self.workers,'stop_one') as stop:
            result=self.portal.request('/api/discovery/open','POST',{'id':'s-abc'})
            stop.assert_called_once_with('s-abc')
            self.assertEqual(start.call_args.args[0],'s-abc')
            self.assertEqual(result['url'],'/speaker/s-abc/')
        self.assertEqual(multi.DEVICES['s-abc']['device'],'192.168.1.30:5555')

    def test_open_cannot_target_arbitrary_address(self):
        with self.assertRaises(ValueError),mock.patch.object(self.workers,'start_one') as start:
            self.portal.request('/api/discovery/open','POST',{'id':'http://example.com'})
        start.assert_not_called()
