# test-lane: fast
"""DGN-1854 W1: fake Google HTTP server, fixture HOME, real store locks."""
import base64
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import multiprocessing
import os
from pathlib import Path
import socket
import stat
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from service import google
from service.google import flow, migration, store, transport
from service.google.errors import GoogleAuthError, GoogleHttpError, NeedsConsent, KEYS, redact, render
from service.google.__main__ import main

FAKE_CLIENT = {'client_id': 'TEST-ONLY-desktop', 'client_secret': 'TEST-ONLY-secret'}


def refresh_worker(_):
    return google.get_access_token(['calendar', 'tasks'])


class GoogleFixture(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def send(self, code, value):
        body = json.dumps(value).encode()
        self.send_response(code)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        s = self.server
        fields = parse_qs(self.rfile.read(int(self.headers['Content-Length'])).decode())
        if self.path == '/revoke':
            s.revoked.append(fields)
            return self.send(200, {})
        s.posts.append(fields)
        if s.failure:
            return self.send(*s.failure)
        if fields['grant_type'] == ['refresh_token']:
            time.sleep(.02)
            payload = {'access_token': 'TEST-ONLY-access', 'expires_in': 3600, 'scope': ' '.join(google.scope_urls(['calendar','tasks']))}
        else:
            code = fields['code'][0]
            if code in s.used_codes:
                return self.send(400, {'error': 'invalid_grant'})
            s.used_codes.add(code)
            verifier = fields['code_verifier'][0]
            if flow._b64(hashlib.sha256(verifier.encode()).digest()) != s.challenge:
                return self.send(400, {'error': 'invalid_grant'})
            claims = {'sub': s.sub, 'email': s.sub + '@example.test', 'aud': FAKE_CLIENT['client_id'],
                      'iss': 'https://accounts.google.com', 'exp': time.time()+3600, 'nonce': s.nonce}
            payload = {'access_token': 'TEST-ONLY-consented', 'refresh_token': 'TEST-ONLY-refresh',
                       'id_token': 'header.' + flow._b64(json.dumps(claims).encode()) + '.signature',
                       'scope': ' '.join(s.granted), 'expires_in': 3600}
        self.send(200, payload)

    def do_GET(self):
        s = self.server
        s.paths.append(self.path)
        if self.path == '/userinfo':
            return self.send(200, {'sub': s.sub, 'email': s.sub + '@example.test'})
        return self.send(s.http_code, s.http_payload)


class CoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='dgn1854-')
        self.env = patch.dict(os.environ, {'HOME': self.tmp.name, 'PROJECT_ROOT': ''})
        self.env.start()
        self.client = patch.object(google, 'client', return_value=FAKE_CLIENT)
        self.client.start()
        self.flow_client = patch.object(flow, 'client', return_value=FAKE_CLIENT)
        self.flow_client.start()
        self.server = ThreadingHTTPServer(('127.0.0.1',0), GoogleFixture)
        s = self.server
        s.posts, s.revoked, s.paths = [], [], []
        s.failure = None
        s.used_codes = set()
        s.sub, s.nonce, s.challenge = 'alice', '', ''
        s.granted = google.scope_urls(['calendar','tasks'])
        s.http_code, s.http_payload = 200, {'ok': True}
        base = 'http://127.0.0.1:%d' % s.server_port
        self.urls = patch.multiple(transport, TOKEN_URL=base+'/token', REVOKE_URL=base+'/revoke', USERINFO_URL=base+'/userinfo')
        self.urls.start()
        self.thread = threading.Thread(target=s.serve_forever, kwargs={'poll_interval':.01})
        self.thread.start()
        self.addCleanup(self.cleanup)

    def cleanup(self):
        self.server.shutdown()
        self.thread.join()
        self.server.server_close()
        self.urls.stop()
        self.flow_client.stop()
        self.client.stop()
        self.env.stop()
        self.tmp.cleanup()

    def seed(self, sub='alice', foreign=False):
        with store.lock():
            rec = {'sub':sub, 'email':sub+'@example.test', 'client_id': FAKE_CLIENT['client_id'],
                   'client_ref':'dogany','refresh_token':'TEST-ONLY-refresh', 'granted':google.scope_urls(['calendar','tasks']),
                   'source':'dogany','created':1,'updated':1}
            if foreign:
                rec.update(client_id='TEST-ONLY-foreign',client_ref='accounts/foreign.client',source='gws-import')
                store.write(store.root()/'accounts/foreign.client', {'client_id':'TEST-ONLY-foreign','client_secret':'TEST-ONLY-secret'})
            store.write(store.root()/'accounts'/(sub+'.json'),rec)
            store.first_default(sub)
        return rec

    def prepare(self, scopes=('calendar','tasks'), account=None):
        result = flow.prepare(scopes, 'fixture:1', 9876, account)
        params = parse_qs(urlsplit(result['url']).query)
        self.server.challenge, self.server.nonce = params['code_challenge'][0], params['nonce'][0]
        self.server.granted = params['scope'][0].split()
        return result, params

    def finish_url(self, result):
        return 'http://127.0.0.1:9876/?state='+result['flow_id']+'&code=TEST-ONLY-'+result['flow_id']

    def finish(self, result):
        return flow.finish(self.finish_url(result), 'fixture:1')

    def assert_class(self, cls, fn, *args, **kwargs):
        with self.assertRaises(GoogleAuthError) as caught:
            fn(*args, **kwargs)
        self.assertEqual(caught.exception.cls,cls)

    def test_status_no_store_no_token(self):
        self.assertFalse(google.status()['connected'])
        self.assertFalse(store.root().exists())
        self.seed()
        out = io.StringIO()
        with patch('sys.stdout',out):
            self.assertEqual(main(['status','--json']),0)
        self.assertNotIn('TEST-ONLY-refresh',out.getvalue())
        self.assertEqual(json.loads(out.getvalue())['sub'],'alice')

    def test_permissions_consent_refresh(self):
        result,_ = self.prepare()
        self.assertTrue(self.finish(result)['ok'])
        self.assertEqual(google.get_access_token(['calendar']),'TEST-ONLY-access')
        for p in store.root().rglob('*'):
            self.assertEqual(stat.S_IMODE(p.stat().st_mode),0o700 if p.is_dir() else 0o600, str(p))
        self.assertEqual(stat.S_IMODE(store.root().stat().st_mode),0o700)

    def test_twenty_processes_one_refresh(self):
        self.seed()
        with ProcessPoolExecutor(20, mp_context=multiprocessing.get_context('fork')) as pool:
            tokens = list(pool.map(refresh_worker,range(20)))
        self.assertEqual(tokens,['TEST-ONLY-access']*20)
        self.assertEqual(len(self.server.posts),1)

    def test_finish_race_exactly_once(self):
        result,_ = self.prepare()
        with ThreadPoolExecutor(20) as pool:
            results = list(pool.map(lambda _: self.finish(result),range(20)))
        self.assertEqual(sum(x['ok'] for x in results),1)
        self.assertEqual(sum(x['class']=='flow_done' for x in results),19)
        self.assertEqual(len(self.server.posts),1)
        self.assertEqual(len(list((store.root()/'accounts').glob('*.json'))),1)

    def test_wrong_state_origin_and_expiry(self):
        result,_ = self.prepare()
        self.assert_class('flow_unknown',flow.finish,self.finish_url(result),'fixture:2')
        self.assert_class('flow_unknown',flow.finish,self.finish_url(result).replace(result['flow_id'],'unknown'),'fixture:1')
        with store.lock():
            p=store.root()/'pending'/(result['flow_id']+'.json')
            pending=store.read(p); pending['expires_at']=time.time()-1; store.write(p,pending)
        self.assert_class('flow_expired',self.finish,result)
        self.assertEqual(self.server.posts,[])
        self.assertTrue(p.exists())

    def test_account_mismatch_and_default(self):
        original=self.seed()
        result,_=self.prepare(account='alice')
        self.server.sub='bob'
        self.assertEqual(self.finish(result)['class'],'account_mismatch')
        self.assertEqual(store.record(),original)
        self.assertEqual(store.default(),'alice')
        self.assertEqual(len(self.server.revoked),1)

    def test_second_account_does_not_change_default(self):
        self.seed()
        result,_=self.prepare()
        self.server.sub='bob'
        self.assertTrue(self.finish(result)['ok'])
        self.assertEqual(store.default(),'alice')
        self.assertEqual(store.record('bob')['sub'],'bob')

    def test_partial_never_replaces_record(self):
        original=self.seed()
        result,_=self.prepare(['gmail.send'],account='alice')
        self.server.granted=google.scope_urls(['gmail.send'])
        self.assertEqual(self.finish(result)['class'],'scope_partial')
        self.assertEqual(store.record(),original)
        self.assertEqual(len(self.server.revoked),0)

    def test_cross_client_full_union(self):
        self.seed(foreign=True)
        result,params=self.prepare(['gmail.send'],account='alice')
        self.assertNotIn('include_granted_scopes',params)
        self.assertEqual(params['prompt'],['consent'])
        self.assertTrue(set(google.scope_urls(['calendar','tasks','gmail.send'])).issubset(params['scope'][0].split()))
        self.assertTrue(self.finish(result)['ok'])
        self.assertEqual(store.record()['source'],'dogany')
        self.assertEqual(store.record()['client_id'],FAKE_CLIENT['client_id'])

    def test_cache_failure_returns_memory_without_fallback(self):
        self.seed()
        original=store.write
        def unwritable(path,*args,**kwargs):
            if path.suffix=='.atok': raise GoogleAuthError('store_unwritable')
            return original(path,*args,**kwargs)
        before={str(p) for p in Path(self.tmp.name).rglob('*')}
        with patch.object(store,'write',side_effect=unwritable):
            self.assertEqual(google.get_access_token(['calendar']),'TEST-ONLY-access')
        after={str(p) for p in Path(self.tmp.name).rglob('*')}
        self.assertEqual(after-before,{str(store.root()/'log/google.log')})

    def test_record_failure_revokes_no_fallback(self):
        result,_=self.prepare()
        original=store.write
        def unwritable(path,*args,**kwargs):
            if path.parent.name=='accounts': raise GoogleAuthError('store_unwritable')
            return original(path,*args,**kwargs)
        before={str(p) for p in Path(self.tmp.name).rglob('*')}
        with patch.object(store,'write',side_effect=unwritable):
            self.assertEqual(self.finish(result)['class'],'store_unwritable')
        self.assertEqual(len(self.server.revoked),1)
        self.assertEqual(before,{str(p) for p in Path(self.tmp.name).rglob('*')})
        self.assertIsNone(store.record())

    def test_probe_failure_no_url_or_spawn(self):
        with patch.object(store,'probe',side_effect=GoogleAuthError('store_unwritable')),patch.object(flow.subprocess,'Popen') as spawn:
            self.assert_class('store_unwritable',flow.start,['calendar'],'fixture:1')
        spawn.assert_not_called()
        self.assertEqual(list((store.root()/'pending').glob('*.json')),[])

    def test_revocation_pause_once_recovery(self):
        self.seed()
        self.server.failure=(400,{'error':'invalid_grant','error_description':'PRIVATE-GOOGLE-TEXT'})
        self.assert_class('token_revoked',google.get_access_token,['calendar'])
        marker=store.read(store.root()/'paused/alice')
        self.assert_class('token_revoked',google.get_access_token,['calendar'])
        self.assertEqual(store.read(store.root()/'paused/alice'),marker)
        self.assertEqual(google.status()['paused'],'token_revoked')
        self.server.failure=None
        google.get_access_token(['calendar'])
        self.assertIsNone(google.status()['paused'])

    def test_http_parity_and_exact_url(self):
        self.seed()
        base='http://127.0.0.1:%d' % self.server.server_port
        for code,reason in ((410,'gone'),(403,'rateLimitExceeded'),(403,'forbidden'),(404,'notFound'),(409,'duplicate')):
            self.server.http_code=code
            self.server.http_payload={'error':{'errors':[{'reason':reason}]}}
            with self.assertRaises(GoogleHttpError) as caught:
                google.call('GET',base+'/events/a%2Fb?syncToken=a%2Bb',scopes=['calendar'])
            self.assertEqual((caught.exception.code,caught.exception.reason),(code,reason))
            self.assertEqual(self.server.paths[-1],'/events/a%2Fb?syncToken=a%2Bb')
        self.server.http_code=412
        self.assertEqual(google.call('GET',base+'/events',scopes=['calendar'])[0],412)

    def test_copy_and_redaction(self):
        for key in KEYS.values():
            for locale in ('en','ko'):
                self.assertTrue(render(key,locale,email='a@example.test'))
        result,_=self.prepare()
        url=self.finish_url(result).replace('code=TEST-ONLY-'+result['flow_id'],'error=access_denied&error_description=PRIVATE-GOOGLE-TEXT')
        result=flow.finish(url,'fixture:1')
        self.assertEqual(result,{'ok':False,'class':'consent_denied'})
        self.assertNotIn('PRIVATE-GOOGLE-TEXT',flow.ERROR_PAGE.decode())
        raw='ya29.'+'PRIVATE 1'+'//PRIVATE code=PRIVATE GOCSPX-'+'PRIVATE'
        cleaned=redact(raw)
        for fragment in ('ya29.','1//','code=','GOCSPX-','PRIVATE'):
            self.assertNotIn(fragment,cleaned)

    def test_cap_prompt_once_and_pause(self):
        for _ in range(2):
            result,_=self.prepare()
            with store.lock():
                p=store.root()/'pending'/(result['flow_id']+'.json')
                rec=store.read(p); rec['expires_at']=time.time()-1; store.write(p,rec)
        self.assertEqual(flow.expired_prompt('fixture:1'),'google.login.blocked_screen_ask')
        self.assertIsNone(flow.expired_prompt('fixture:1'))
        self.assertTrue(flow.record_blocked_screen('fixture:1',True)['operator_alert'])
        self.assertEqual(google.status()['paused'],'cap_suspected')
        self.assert_class('cap_suspected',self.prepare)

    def fixture_gws(self):
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        directory=Path.home()/'.config/gws'; directory.mkdir(parents=True)
        key=AESGCM.generate_key(128); nonce=os.urandom(12)
        (directory/'.encryption_key').write_bytes(base64.b64encode(key))
        (directory/'credentials.enc').write_bytes(nonce+AESGCM(key).encrypt(nonce,json.dumps({'refresh_token':'TEST-ONLY-legacy'}).encode(),None))
        (directory/'client_secret.json').write_text(json.dumps({'installed':FAKE_CLIENT}))
        return directory

    def test_import_readonly_exclusive_dryrun(self):
        directory=self.fixture_gws()
        before={p.name:p.read_bytes() for p in directory.iterdir()}
        self.assertEqual(migration.import_gws(True)['class'],'dry_run')
        self.assertFalse(store.root().exists())
        self.assertTrue(migration.import_gws()['imported'])
        original=store.record()
        self.assertEqual(original['source'],'gws-import')
        self.assertFalse(migration.import_gws()['imported'])
        self.assertEqual(store.record(),original)
        self.assertEqual(before,{p.name:p.read_bytes() for p in directory.iterdir()})
        google.get_access_token(['calendar'])
        self.assertEqual(self.server.posts[-1]['client_id'],[FAKE_CLIENT['client_id']])

    def test_import_rotation_busy(self):
        directory=self.fixture_gws()
        (directory/'credentials.enc').write_bytes(os.urandom(40))
        with patch.object(migration.time,'sleep') as sleep:
            self.assert_class('import_busy',migration.read_gws)
        self.assertEqual([call.args[0] for call in sleep.call_args_list],[1,2,4])
        self.assertFalse(store.root().exists())

    def test_import_connect_refresh_race(self):
        self.fixture_gws()
        result,_=self.prepare()
        def operation(i):
            if i%3==0: return self.finish(result)
            if i%3==1: return migration.import_gws()
            try: return google.get_access_token(['calendar'])
            except NeedsConsent: return None
        with ThreadPoolExecutor(20) as pool:
            list(pool.map(operation,range(20)))
        self.assertEqual(store.record()['source'],'dogany')
        self.assertEqual(len(list((store.root()/'accounts').glob('*.json'))),1)
        self.assertEqual(sum(p['grant_type']==['authorization_code'] for p in self.server.posts),1)

    def test_set_default_and_disconnect(self):
        self.seed(); self.seed('bob')
        self.assertEqual(store.default(),'alice')
        google.set_default('bob')
        self.assertEqual(store.default(),'bob')
        google.disconnect()
        self.assertFalse(google.status()['connected'])
        self.assertEqual(len(list((store.root()/'_archive').glob('bob-*.json'))),1)
        self.assertEqual(len(self.server.revoked),1)

    def test_reused_code_in_new_flow_rejected(self):
        first,_=self.prepare()
        self.assertTrue(self.finish(first)['ok'])
        second,_=self.prepare(account='alice')
        reused=self.finish_url(second).replace('code=TEST-ONLY-'+second['flow_id'], 'code=TEST-ONLY-'+first['flow_id'])
        self.assertEqual(flow.finish(reused,'fixture:1')['class'],'flow_expired')

    def test_listener_and_completion_relay(self):
        instance=Path(self.tmp.name)/'fixture'
        instance.mkdir()
        sock=socket.socket()
        sock.bind(('127.0.0.1',0)); sock.listen(5)
        with patch.dict(os.environ,{'PROJECT_ROOT':str(instance)}):
            result=flow.prepare(['calendar','tasks'],'fixture:1',sock.getsockname()[1])
        params=parse_qs(urlsplit(result['url']).query)
        self.server.challenge=params['code_challenge'][0]
        self.server.nonce=params['nonce'][0]
        self.server.granted=params['scope'][0].split()
        port=sock.getsockname()[1]
        fd=sock.detach()
        listener=threading.Thread(target=flow.listen,args=(result['flow_id'],fd))
        listener.start()
        try:
            from urllib.request import urlopen
            url='http://127.0.0.1:%d/?state=%s&code=TEST-ONLY-listener' % (port,result['flow_id'])
            with urlopen(url,timeout=5) as response:
                self.assertEqual(response.read(),flow.SUCCESS_PAGE)
        finally:
            # If callback failed, close the loop without leaving a worker running.
            with store.lock():
                path=store.root()/'pending'/(result['flow_id']+'.json')
                pending=store.read(path); pending['done']=True; store.write(path,pending)
            listener.join(5)
        self.assertFalse(listener.is_alive())
        notices=list((instance/'.telegram_bot/session-inbox').glob('*.md'))
        self.assertEqual(len(notices),1)
        text=notices[0].read_text()
        self.assertIn('[google-connect-result] class=ok sub=alice origin=fixture:1',text)
        self.assertNotIn('TEST-ONLY',text)

    def test_start_passes_bound_socket_and_no_credentials(self):
        with patch.object(flow.subprocess,'Popen') as spawn:
            result=flow.start(['calendar'],'fixture:1')
        args,kwargs=spawn.call_args
        self.assertEqual(args[0][1:4],['-m','service.google','_listen'])
        self.assertEqual(args[0][4],result['flow_id'])
        self.assertEqual(len(kwargs['pass_fds']),1)
        self.assertNotIn('TEST-ONLY',str(args))

    def test_token_cli_cache_failure_keeps_expiry(self):
        self.seed()
        original=store.write
        def unwritable(path,*args,**kwargs):
            if path.suffix=='.atok': raise GoogleAuthError('store_unwritable')
            return original(path,*args,**kwargs)
        out=io.StringIO()
        with patch.object(store,'write',side_effect=unwritable),patch('sys.stdout',out):
            self.assertEqual(main(['token','--scopes','calendar','--json']),0)
        result=json.loads(out.getvalue())
        self.assertEqual(result['access_token'],'TEST-ONLY-access')
        self.assertGreater(result['expires_at'],time.time()+120)

    def test_lock_timeout(self):
        with store.lock():
            self.assert_class('store_busy',lambda: store.lock(.04).__enter__())


if __name__=='__main__':
    unittest.main(verbosity=2)
