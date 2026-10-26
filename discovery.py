"""Bounded LAN discovery of rooted Quasar speakers; no global adb reconnects."""
import concurrent.futures
import hashlib
import ipaddress
import json
import os
import re
import socket
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path

PRIVATE = tuple(ipaddress.ip_network(n) for n in ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16', '169.254.0.0/16'))
INTERVAL = 120
MAX_HOSTS = 1024
# Read only an identifying field, never the full Quasar config or account data.
IDENTIFY = r'''if [ -x /system/vendor/quasar/maind ]; then printf '\nPANEL_STATION\n'; sed -n 's/.*"deviceType"[[:space:]]*:[[:space:]]*"\([a-zA-Z0-9_-]*\)".*/\1/p' /system/vendor/quasar/quasar.cfg | head -1; printf '\n'; cat /sys/class/net/wlan0/address 2>/dev/null; printf '\n'; hostname; fi'''

def local_ip(value):
    try:
        ip = ipaddress.IPv4Address(value)
        return ip if any(ip in n for n in PRIVATE) else None
    except ValueError:
        return None

def endpoint(value):
    host, sep, port = str(value or '').rpartition(':')
    if not sep: host, port = str(value or ''), '5555'
    if local_ip(host) and port.isdecimal() and 1 <= int(port) <= 65535:
        return host, int(port)
    return None

def run(command):
    try:
        return subprocess.run(command, capture_output=True, text=True, timeout=8).stdout
    except (OSError, subprocess.TimeoutExpired):
        return ''

def interface_networks():
    pairs = []
    if sys.platform == 'darwin':
        name, active = '', False
        for line in run(['/sbin/ifconfig']).splitlines():
            if line and not line[0].isspace():
                name = line.split(':',1)[0]
                active = 'UP' in line and not name.startswith(('lo', 'utun', 'awdl', 'llw', 'bridge'))
            m = re.search(r'\binet (\d+\.\d+\.\d+\.\d+) netmask (0x[0-9a-f]+|[\d.]+)', line)
            if active and m:
                mask = str(ipaddress.IPv4Address(int(m[2],16))) if m[2].startswith('0x') else m[2]
                pairs.append((m[1],mask))
    elif os.name == 'nt':
        script = 'Get-NetIPConfiguration | Where-Object {$_.NetAdapter.Status -eq "Up" -and $_.IPv4DefaultGateway} | ForEach-Object {$_.IPv4Address | Select-Object IPAddress,PrefixLength} | ConvertTo-Json -Compress'
        try:
            rows = json.loads(run(['powershell.exe','-NoProfile','-NonInteractive','-Command',script]))
            for row in rows if isinstance(rows,list) else [rows]: pairs.append((row['IPAddress'],row['PrefixLength']))
        except (ValueError, KeyError, TypeError): pass
    else:
        try:
            for row in json.loads(run(['ip','-j','-4','address','show','up'])):
                if row.get('ifname','').startswith(('lo','docker','veth','br-','tun','wg','tailscale')): continue
                for addr in row.get('addr_info',[]):
                    if addr.get('family')=='inet': pairs.append((addr['local'],addr['prefixlen']))
        except (ValueError, KeyError, TypeError): pass
    networks = []
    for host, mask in pairs:
        if not local_ip(host): continue
        try:
            net = ipaddress.ip_network(f'{host}/{mask}', strict=False)
            # Do not sweep a corporate /16 automatically. Start with the local /24.
            if net.num_addresses > 256: net = ipaddress.ip_network(host+'/24',strict=False)
            if str(net) not in networks: networks.append(str(net))
        except ValueError: pass
    return networks

def validate_networks(values):
    if not isinstance(values,list) or len(values)>4: raise ValueError('Укажите не больше четырёх локальных сетей')
    result = []
    for value in values:
        try: net = ipaddress.IPv4Network(value,strict=False)
        except (ValueError, TypeError): raise ValueError('Нужна локальная IPv4-сеть, например 192.168.1.0/24') from None
        if not any(net.subnet_of(n) for n in PRIVATE): raise ValueError('Поиск доступен только в локальных IPv4-сетях')
        if str(net) not in result: result.append(str(net))
    if sum(ipaddress.ip_network(n).num_addresses for n in result)>MAX_HOSTS: raise ValueError('В одном поиске допускается не больше 1024 адресов')
    return result

def validate_ports(values):
    if not isinstance(values,list) or not 1<=len(values)<=4: raise ValueError('Укажите от одного до четырёх портов ADB')
    if any(type(p) is not int or not 1<=p<=65535 for p in values): raise ValueError('Порт ADB должен быть числом от 1 до 65535')
    return list(dict.fromkeys(values))

def packet(command, arg0=0, arg1=0, data=b''):
    code = int.from_bytes(command,'little')
    return struct.pack('<6I',code,arg0,arg1,len(data),sum(data)&0xffffffff,code^0xffffffff)+data

def receive(sock, deadline):
    def exact(size):
        out=b''
        while len(out)<size:
            remaining=deadline-time.monotonic()
            if remaining<=0: raise TimeoutError()
            sock.settimeout(remaining)
            part=sock.recv(size-len(out))
            if not part: raise ConnectionError()
            out+=part
        return out
    code,a,b,size,checksum,magic=struct.unpack('<6I',exact(24))
    if magic!=code^0xffffffff or size>65536: raise ValueError('Invalid ADB frame')
    data=exact(size)
    if sum(data)&0xffffffff!=checksum: raise ValueError('Invalid ADB checksum')
    return code.to_bytes(4,'little'),a,b,data

def probe(host, port):
    """Only CNXN + a fixed read-only shell probe. AUTH devices are not auto-authorized."""
    try:
        with socket.create_connection((host,port),timeout=.8) as sock:
            deadline=time.monotonic()+3
            sock.sendall(packet(b'CNXN',0x01000000,4096,b'host::\0'))
            command,_,_,_=receive(sock,deadline)
            if command!=b'CNXN': return None
            sock.sendall(packet(b'OPEN',1,0,('shell:'+IDENTIFY+'\0').encode()))
            output=b''
            while len(output)<4096:
                command,remote,local,data=receive(sock,deadline)
                if local!=1: return None
                if command==b'WRTE':
                    output+=data
                    sock.sendall(packet(b'OKAY',1,remote))
                elif command==b'CLSE': break
                elif command!=b'OKAY': return None
            text=output.decode('utf8','replace')
            if '\nPANEL_STATION\n' not in text.replace('\r',''): return None
            lines=[s.strip() for s in text.replace('\r','').split('\nPANEL_STATION\n',1)[1].splitlines() if s.strip()]
            platform=next((s for s in lines if re.fullmatch(r'[a-zA-Z0-9_-]+',s)), '')
            mac=next((s for s in lines if re.fullmatch(r'(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}',s)), '')
            from profiles import PROFILES
            model=next((key for key,p in PROFILES.items() if p['platform']==platform),'lite2')
            name='Станция Лайт' if platform=='yandexmicro' else PROFILES[model]['name'] if platform in {p['platform'] for p in PROFILES.values()} else 'Яндекс Станция'
            address=f'{host}:{port}'
            identifier='s-'+hashlib.sha256((mac.lower() or address).encode()).hexdigest()[:12]
            return {'id':identifier,'address':address,'name':name,'model':model,'platform':platform,'available':True,'last_seen':time.time()}
    except (OSError, ValueError, struct.error): return None

class Scanner:
    def __init__(self, path, known=()):
        self.path=Path(path);self.known=list(known);self.lock=threading.RLock();self.stop_event=threading.Event()
        self.devices={};self.scanning=False;self.checked=0;self.total=0;self.updated=0;self.error=''
        self.networks=[];self.ports=list(dict.fromkeys([5555]+[ep[1] for a in self.known if (ep:=endpoint(a))]))[:4];self.auto=True
        try:
            saved=json.loads(self.path.read_text())
            self.networks=validate_networks(saved.get('networks',[]));self.ports=validate_ports(saved.get('ports',self.ports));self.auto=bool(saved.get('auto',True))
        except (OSError,ValueError,TypeError):pass

    def targets(self):
        networks=list(self.networks)
        if not networks:
            networks=interface_networks()
            for address in self.known:
                ep=endpoint(address)
                if ep:
                    net=str(ipaddress.ip_network(ep[0]+'/24',strict=False))
                    if net not in networks:networks.append(net)
            networks=networks[:4]
        targets={(str(ip),p) for n in networks for ip in ipaddress.ip_network(n).hosts() for p in self.ports}
        return networks,sorted(targets)

    def snapshot(self):
        with self.lock:
            return {'scanning':self.scanning,'checked':self.checked,'total':self.total,'updated':self.updated,
                    'networks':list(getattr(self,'active_networks',self.networks)), 'configured_networks':list(self.networks),
                    'ports':list(self.ports),'auto':self.auto,'error':self.error,
                    'devices':sorted((dict(d) for d in self.devices.values()),key=lambda d:(not d['available'],d['address']))}

    def configure(self, data):
        if not isinstance(data,dict):raise ValueError('Ожидался объект параметров поиска')
        networks=validate_networks(data.get('networks',[]));ports=validate_ports(data.get('ports',[5555]))
        if type(data.get('auto',True)) is not bool:raise ValueError('Некорректный режим автопоиска')
        with self.lock:
            if self.scanning:raise ValueError('Дождитесь окончания текущего поиска')
            self.path.parent.mkdir(parents=True,exist_ok=True)
            temporary=self.path.with_suffix('.tmp')
            with open(temporary,'w',encoding='utf8',opener=lambda p,f:os.open(p,f,0o600)) as f:
                json.dump({'networks':networks,'ports':ports,'auto':data.get('auto',True)},f)
            os.replace(temporary,self.path)
            self.networks,self.ports,self.auto=networks,ports,data.get('auto',True)

    def start(self):
        with self.lock:
            if self.scanning or self.stop_event.is_set():return self.snapshot()
            self.scanning=True;self.checked=0;self.total=0;self.error=''
        threading.Thread(target=self._scan,daemon=True,name='speaker-discovery').start()
        return self.snapshot()

    def _scan(self):
        try:
            networks,targets=self.targets()
            with self.lock:self.active_networks=networks;self.total=len(targets)
            if not targets:raise ValueError('Не удалось определить локальную сеть. Укажите сеть в параметрах поиска.')
            found={}
            with concurrent.futures.ThreadPoolExecutor(max_workers=32) as pool:
                futures={pool.submit(probe,*ep) for ep in targets}
                for future in concurrent.futures.as_completed(futures):
                    if self.stop_event.is_set():
                        for pending in futures:pending.cancel()
                        break
                    device=future.result()
                    with self.lock:
                        self.checked+=1
                        if device:
                            found[device['id']]=device;self.devices[device['id']]=device
            with self.lock:
                for identifier,d in self.devices.items():d['available']=identifier in found
                self.updated=time.time()
        except ValueError as error:
            with self.lock:self.error=str(error)
        except Exception:
            with self.lock:self.error='Не удалось завершить поиск. Проверьте локальную сеть и параметры поиска.'
        finally:
            with self.lock:self.scanning=False

    def run(self):
        while not self.stop_event.is_set():
            if self.auto:self.start()
            self.stop_event.wait(INTERVAL)

    def resolve(self, identifier):
        if not isinstance(identifier,str):raise ValueError('Некорректный идентификатор колонки')
        with self.lock:
            d=self.devices.get(identifier)
            if not d or not d['available'] or time.time()-d['last_seen']>2*INTERVAL:raise ValueError('Колонка пока недоступна. Повторите поиск.')
            return dict(d)
