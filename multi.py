"""One authenticated LAN console, explicit per-device routes, private loopback workers."""
import http.client
import ipaddress
import json
import os
import select
import socket
import threading
import argparse
import re
import secrets
import signal
import ssl
import subprocess
import sys
import tempfile
import time
from http.cookies import SimpleCookie
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit
import access
import server
import discovery

DEVICES = {}
ROOT = Path(__file__).resolve().parent
HOP={'connection','keep-alive','proxy-authenticate','proxy-authorization','te','trailer','transfer-encoding','upgrade','content-length','server','date','set-cookie'}
access.COOKIE = 'yl2_session'

def route(path):
    parsed=urlsplit(path)
    if parsed.scheme or parsed.netloc:raise ValueError('Требуется локальный путь')
    parts=parsed.path.split('/',3)
    if len(parts)<4 or parts[1]!='speaker' or parts[2] not in DEVICES:
        raise ValueError('Неизвестная колонка')
    model=parts[2]
    return model,'/'+parts[3]+('?' + parsed.query if parsed.query else '')

class GatewayRoutes:
    secret=''
    ports = {}
    networks = []
    data_dir = None
    discovery = None
    def _panel_auth_get(self):
        return {**self.access.info(self._token()), 'secure':self.https, 'ports':self.ports, 'ca':(self.data_dir/'.tls/ca.crt').exists()}

    def _allowed(self,path):
        if self.networks and not any(ipaddress.ip_address(self.client_address[0]) in network for network in self.networks):
            self._send(403,{'error':'Доступ только из домашней сети'});return False
        return not server.Handler._guard(self,path)

    def do_GET(self):self.dispatch()
    def do_POST(self):self.dispatch()
    def do_HEAD(self):self._send(405,{'error':'Метод не поддерживается'})

    def dispatch(self):
        try:
            if self.path=='/' or self.path.startswith('/?'):
                if not self._allowed('/'):return
                cookies=SimpleCookie()
                try:cookies.load(self.headers.get('Cookie',''))
                except Exception:pass
                selected=cookies.get('panel_device')
                model=selected.value if selected and selected.value in DEVICES else next(iter(DEVICES))
                return self._send(302,b'',headers=[('Location','/speaker/'+model+'/')])
            try:model,path=route(self.path)
            except ValueError:
                return self._send(409,{'error':'Обновите страницу единой панели','url':'/'})
            clean=urlsplit(path).path
            if not self._allowed(clean):return
            if clean.startswith('/api/discovery') and self.discovery:
                return self._handle(lambda:self.discovery.request(clean,self.command,self._json() if self.command=='POST' else {}))
            if clean=='/api/panel-auth' and self.command=='GET':
                return self._send(200,self._panel_auth_get())
            if clean.startswith('/api/panel-auth/') and self.command=='POST':
                return self._panel_auth_post(clean,self._json())
            if clean=='/api/speakers' and self.command=='GET':
                return self._send(200,{'selected':model,'devices':[{'id':key,'name':value['name']}for key,value in list(DEVICES.items())]})
            if clean=='/manifest.webmanifest' and self.command=='GET':
                manifest=json.loads((server.STATIC/'manifest.webmanifest').read_text(encoding='utf8'))
                manifest.update(name=DEVICES[model]['name'],short_name=DEVICES[model]['name'])
                return self._send(200,json.dumps(manifest,ensure_ascii=False).encode('utf8'),server.MIME['.webmanifest'])
            if clean == '/api/panel/update' and self.command == 'POST':
                return self._send(400, {'error':'Обновите исходники и перезапустите панель целиком; обновление отдельной колонки отключено.'})
            if clean == '/ca.crt':
                certificate = self.data_dir/'.tls/ca.crt'
                if not certificate.exists(): return self._send(404, {'error':'HTTPS не включён'})
                return self._send(200,certificate.read_bytes(),'application/x-x509-ca-cert')
            if self.headers.get('Upgrade','').lower()=='websocket':
                if self.discovery:self.discovery.ensure_worker(model)
                return self.tunnel(model,path)
            if self.discovery:self.discovery.ensure_worker(model)
            self.proxy(model,path)
        except (BrokenPipeError,ConnectionResetError):pass
        except (OSError,http.client.HTTPException,ValueError,RuntimeError) as error:
            self._send(502,{'error':'Не удалось связаться с выбранной колонкой','detail':str(error)[:180]})

    def request_headers(self):
        return {**{key:value for key,value in self.headers.items() if key.lower() not in HOP and key.lower()!='x-panel-gateway'},'X-Panel-Gateway':self.secret}

    def proxy(self,model,path):
        length=int(self.headers.get('Content-Length') or 0)
        if length<0 or length>512*1024*1024:return self._send(413,{'error':'Слишком большой запрос'})
        body=self.rfile.read(length) if length else None
        conn=http.client.HTTPConnection('127.0.0.1',DEVICES[model]['port'],timeout=180)
        try:
            conn.request(self.command,path,body=body,headers=self.request_headers())
            response=conn.getresponse()
            self.send_response(response.status)
            for key,value in response.getheaders():
                if key.lower() not in HOP:self.send_header(key,value)
            if urlsplit(path).path in ('/','/index.html'):
                self.send_header('Set-Cookie',f'panel_device={model}; Path=/; SameSite=Strict; Max-Age=31536000')
            size=response.getheader('Content-Length')
            if size is not None:self.send_header('Content-Length',size)
            self.send_header('Connection','close');self.end_headers();self.close_connection=True
            while chunk:=response.read(65536):self.wfile.write(chunk)
        finally:conn.close()

    def tunnel(self,model,path):
        # Authenticate before connecting and keep the selected worker fixed for the whole socket.
        origin=self.headers.get('Origin')
        if not origin or urlsplit(origin).netloc!=self.headers.get('Host'):
            return self._send(403,{'error':'Запрос консоли с чужого сайта'})
        with socket.create_connection(('127.0.0.1',DEVICES[model]['port']),timeout=15) as upstream:
            headers=self.request_headers();headers.update({'Upgrade':'websocket','Connection':'Upgrade'})
            packet=f'GET {path} HTTP/1.1\r\n'+''.join(f'{key}: {value}\r\n'for key,value in headers.items())+'\r\n'
            upstream.sendall(packet.encode('latin1'))
            received=b''
            while b'\r\n\r\n' not in received:
                chunk=upstream.recv(4096)
                if not chunk:raise OSError('Консоль закрыла соединение')
                received+=chunk
                if len(received)>65536:raise OSError('Некорректный ответ консоли')
            self.wfile.write(received);self.wfile.flush();self.close_connection=True
            if b' 101 ' not in received.split(b'\r\n',1)[0]:return
            upstream.settimeout(None)
            while True:
                if not self.access.session_ok(self._token()):return
                ready,_,_=select.select([self.connection,upstream],[],[],5)
                for source in ready:
                    chunk=source.recv(65536)
                    if not chunk:return
                    (upstream if source is self.connection else self.connection).sendall(chunk)


class Gateway(GatewayRoutes, server.Handler):
    pass


def load_config(path):
    from profiles import PROFILES
    value = json.loads(Path(path).read_text(encoding='utf8'))
    speakers = value.get('speakers') if isinstance(value,dict) else None
    if not isinstance(speakers,list) or not 1 <= len(speakers) <= 32:
        raise ValueError('speakers must contain 1..32 devices')
    result = {}
    for item in speakers:
        if not isinstance(item,dict): raise ValueError('invalid speaker')
        identifier = item.get('id','')
        if not isinstance(identifier,str) or not re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,39}',identifier) or identifier in result:
            raise ValueError('speaker ids must be unique lowercase URL-safe names')
        model = item.get('model','lite2')
        if model not in PROFILES: raise ValueError('unknown model')
        device = item.get('device')
        if not isinstance(device,str) or not device.strip() or any(character.isspace() for character in device):
            raise ValueError('device must be an ADB address or USB serial')
        name = item.get('name',PROFILES[model]['name'])
        if not isinstance(name,str) or not 1 <= len(name) <= 80: raise ValueError('invalid speaker name')
        result[identifier] = {'name':name,'model':model,'device':device}
    return result


class Workers:
    def __init__(self,config,data_dir,timeout=30):
        self.config,self.data_dir,self.timeout = config,data_dir,timeout
        self.children=[]
        self.secret=secrets.token_urlsafe(48)
        self.lock=threading.RLock()
        self.running={}
        self.stopped=False

    def start_one(self,identifier,spec):
        with self.lock:
            if self.stopped:raise ValueError('Панель останавливается')
            if identifier in self.running and self.running[identifier].poll() is None:return
            directory=self.data_dir/'speakers'/identifier
            directory.mkdir(parents=True,mode=0o700,exist_ok=True)
            with tempfile.TemporaryDirectory(prefix='panel-ready-') as temporary:
                ready=Path(temporary)/'ready.json'
                env={**os.environ,'PANEL_GATEWAY_SECRET':self.secret,'PANEL_READY_FILE':str(ready)}
                command=[sys.executable,str(ROOT/'server.py'),'--device',spec['device'],'--model',spec['model'],
                         '--host','127.0.0.1','--port','0','--https-port','0','--data-dir',str(directory)]
                with open(directory/'worker.log','ab') as log:
                    process=subprocess.Popen(command,cwd=ROOT,env=env,stdout=log,stderr=subprocess.STDOUT)
                try:
                    deadline=time.monotonic()+self.timeout
                    while not ready.exists():
                        if process.poll() is not None:raise RuntimeError('worker failed: '+identifier+'; see worker.log')
                        if time.monotonic()>deadline:raise RuntimeError('worker startup timeout: '+identifier)
                        time.sleep(.05)
                    spec['port']=json.loads(ready.read_text())['port']
                    self.children.append(process);self.running[identifier]=process
                except BaseException:
                    process.terminate()
                    try:process.wait(timeout=5)
                    except subprocess.TimeoutExpired:process.kill();process.wait()
                    raise

    def start(self):
        try:
            for identifier,spec in self.config.items():self.start_one(identifier,spec)
            return self.secret
        except BaseException:
            self.stop();raise

    def stop_one(self,identifier):
        with self.lock:
            process=self.running.pop(identifier,None)
            if process is None:return
            if process.poll() is None:process.terminate()
            try:process.wait(timeout=10)
            except subprocess.TimeoutExpired:process.kill();process.wait()
            self.children.remove(process)
    def stop(self):
        with self.lock:
            self.stopped=True
            for child in self.children:
                if child.poll() is None: child.terminate()
            for child in self.children:
                try:child.wait(timeout=10)
                except subprocess.TimeoutExpired: child.kill();child.wait()
            self.children.clear()
            self.running.clear()


class DiscoveryPortal:
    def __init__(self,data_dir,workers,root_id=None):
        self.workers,self.root_id=workers,root_id
        self.lock=threading.RLock()
        self.original=set(DEVICES)
        self.file=Path(data_dir)/'data/discovery/opened.json'
        try:
            for key,spec in load_config(self.file).items():
                if key not in DEVICES and len(DEVICES)<32:
                    DEVICES[key]=spec;workers.config[key]=spec
        except (OSError,ValueError):pass
        self.scanner=discovery.Scanner(Path(data_dir)/'data/discovery/settings.json',known=[s.get('device') for s in DEVICES.values()],adb=server.find_adb())
        threading.Thread(target=self.scanner.run,daemon=True,name='discovery-schedule').start()

    def ensure_worker(self,identifier):
        if identifier!=self.root_id:self.workers.start_one(identifier,DEVICES[identifier])

    def request(self,path,method,data):
        if not isinstance(data,dict):raise ValueError('Ожидался объект параметров поиска')
        if path=='/api/discovery' and method=='GET':
            state=self.scanner.snapshot()
            for item in state['devices']:
                match=next((key for key,spec in list(DEVICES.items()) if server.normalize_device(spec.get('device'))==item['address']),None)
                if match:item['url']='/speaker/'+match+'/'
            return state
        if method!='POST':raise ValueError('Неизвестная команда поиска')
        if path=='/api/discovery/scan':return self.scanner.start()
        if path=='/api/discovery/config':
            self.scanner.configure(data)
            return self.scanner.start()
        if path=='/api/discovery/open':
            item=self.scanner.resolve(data.get('id',''))
            with self.lock:
                identifier=next((key for key,spec in list(DEVICES.items()) if server.normalize_device(spec.get('device'))==item['address']),item['id'])
                if identifier not in DEVICES:
                    if len(DEVICES)>=32:raise ValueError('В одной панели поддерживается до 32 колонок')
                    spec={'name':item['name'],'model':item['model'],'device':item['address']}
                    self.workers.start_one(identifier,spec)
                    DEVICES[identifier]=spec;self.workers.config[identifier]=spec
                elif server.normalize_device(DEVICES[identifier].get('device'))!=item['address']:
                    self.workers.stop_one(identifier)
                    spec={**DEVICES[identifier],'device':item['address']}
                    self.workers.start_one(identifier,spec)
                    DEVICES[identifier]=spec;self.workers.config[identifier]=spec
                else:self.ensure_worker(identifier)
                self.file.parent.mkdir(parents=True,exist_ok=True)
                saved={'speakers':[{'id':key,**{k:s[k] for k in ('name','model','device')}} for key,s in list(DEVICES.items()) if key not in self.original]}
                temporary=self.file.with_suffix('.tmp')
                with open(temporary,'w',encoding='utf8',opener=lambda p,f:os.open(p,f,0o600)) as f:json.dump(saved,f,ensure_ascii=False)
                os.replace(temporary,self.file)
                return {'url':'/speaker/'+identifier+'/'}
        raise ValueError('Неизвестная команда поиска')

    def close(self):
        self.scanner.stop_event.set()
        self.workers.stop()


def attach_discovery(base,device,model,data_dir):
    """Keep the existing root panel and give each additional speaker a pinned worker."""
    DEVICES.clear()
    if device:DEVICES['current']={'name': 'Основная колонка', 'model':model, 'device':device, 'port':0}
    workers=Workers({},Path(data_dir)/'data/discovery')
    portal=DiscoveryPortal(data_dir,workers,root_id='current')
    class AutoHandler(GatewayRoutes,base):
        def do_GET(self):self.local_or_device('GET')
        def do_POST(self):self.local_or_device('POST')
        def local_or_device(self,method):
            path=urlsplit(self.path).path
            if path.startswith('/speaker/'):return self.dispatch()
            if path.startswith('/api/discovery') or path=='/api/speakers':
                if self._guard(path):return
                if path=='/api/speakers':
                    return self._send(200,{'selected':'current','devices':[{'id':k,'name':v['name']}for k,v in list(DEVICES.items())]})
                return self._handle(lambda:portal.request(path,method,self._json() if method=='POST' else {}))
            return (base.do_GET if method=='GET' else base.do_POST)(self)
    AutoHandler.discovery=portal
    AutoHandler.secret=workers.secret
    AutoHandler.data_dir=Path(data_dir)
    AutoHandler.ports=base.ports
    return AutoHandler,portal


def main():
    parser=argparse.ArgumentParser(description='One console for several rooted Yandex speakers')
    parser.add_argument('--config',required=True,help='JSON speaker list; see speakers.example.json')
    parser.add_argument('--data-dir',type=Path,default=ROOT/'data/multi')
    parser.add_argument('--host',default='127.0.0.1')
    parser.add_argument('--port',type=int,default=8765)
    parser.add_argument('--https-port',type=int,default=0)
    parser.add_argument('--allow-network',action='append',default=[],help='optional client CIDR; repeat for multiple networks')
    args=parser.parse_args()
    # Workers receive their private key in a copied environment; the gateway never uses worker mode.
    if os.environ.get('PANEL_GATEWAY_SECRET'): parser.error('PANEL_GATEWAY_SECRET is reserved for private workers')
    Gateway.networks=[ipaddress.ip_network(network) for network in args.allow_network]
    config=load_config(args.config);DEVICES.update(config)
    data_dir=args.data_dir.resolve();data_dir.mkdir(parents=True,mode=0o700,exist_ok=True)
    Gateway.data_dir=data_dir;Gateway.access=access.PanelAccess(data_dir/'.panel_auth.json')
    workers=Workers(config,data_dir)
    httpd=tls=None
    signal.signal(signal.SIGTERM,lambda *_:sys.exit(0))
    try:
        Gateway.secret=workers.start()
        Gateway.discovery=DiscoveryPortal(data_dir,workers)
        httpd=ThreadingHTTPServer((args.host,args.port),Gateway);httpd.daemon_threads=True
        Gateway.ports={'http':httpd.server_port}
        if args.https_port:
            certificate,key=access.ensure_certs(data_dir/'.tls')
            context=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER);context.minimum_version=ssl.TLSVersion.TLSv1_2
            context.load_cert_chain(certificate,key)
            tls=server.TLSServer((args.host,args.https_port),Gateway,context);tls.daemon_threads=True
            Gateway.ports['https']=tls.server_port
            threading.Thread(target=tls.serve_forever,daemon=True).start()
        print('Unified panel: http://'+args.host+':'+str(httpd.server_port),flush=True)
        if not Gateway.access.enabled:print('Set a shared panel password in Settings before exposing it to your LAN.',flush=True)
        httpd.serve_forever()
    except KeyboardInterrupt:pass
    finally:
        if Gateway.discovery:Gateway.discovery.scanner.stop_event.set()
        if tls:tls.shutdown();tls.server_close()
        if httpd:httpd.server_close()
        workers.stop()


if __name__=='__main__':main()
