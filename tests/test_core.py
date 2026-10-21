import json
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch
import subprocess
import urllib.error
import urllib.request
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import Application, Handler, Store, ThreadingHTTPServer, metrics, parse_intent
from executor import DesktopExecutor
from meeting_cli import meeting_info


class IntentTests(unittest.TestCase):
    def test_supported_movie(self):
        intent = parse_intent('搜索《流浪地球2》并播放预告片')
        self.assertEqual(intent['movie_title'], '流浪地球2')
        self.assertEqual(intent['kind'], 'movie')

    def test_meeting_codes(self):
        self.assertEqual(parse_intent('加入腾讯会议，会议码1234')['code'], 'invalid_meeting_code')
        self.assertEqual(parse_intent('加入会议 123 456 789')['meeting_code'], '123456789')
        self.assertEqual(parse_intent('加入会议 1234567890')['meeting_code'], '1234567890')

    def test_scope(self):
        self.assertEqual(parse_intent('创建一个腾讯会议')['kind'], 'meeting_create')
        self.assertIn('error', parse_intent('打开会议并播放电影'))
        self.assertIn('error', parse_intent('帮我转账'))
        self.assertIn('error', parse_intent('搜索股票'))
        self.assertIn('error', parse_intent('搜索一部热门电影并播放相关视频'))

    def test_video_allowlist(self):
        self.assertTrue(DesktopExecutor.is_video_url('https://www.bilibili.com/video/BV1test/'))
        for url in ('https://www.bilibili.com.attacker.test/video/BV1test', 'javascript:alert(1)', 'https://bilibili.com/read/123'):
            self.assertFalse(DesktopExecutor.is_video_url(url))

    def test_api_result_shapes(self):
        self.assertEqual(meeting_info({'data':{'meeting_info_list':[{'meeting_id':'1','meeting_code':'123456789'}]}})['meeting_code'], '123456789')
        self.assertIsNone(meeting_info({'success':True}))


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def task(self, mode='mac'):
        return self.store.insert('打开腾讯会议', parse_intent('打开腾讯会议'), mode, '')

    def test_late_success_does_not_overwrite_cancel(self):
        t=self.task();self.store.update(t['id'],'running','领取')
        self.store.update(t['id'],'cancelled','取消')
        self.store.update(t['id'],'succeeded','迟到结果')
        self.assertEqual(self.store.get(t['id'])['status'],'cancelled')

    def test_restart_never_replays_external_actions(self):
        t=self.task();self.store.update(t['id'],'running','领取')
        reopened=Store(self.temp.name)
        self.assertEqual(reopened.get(t['id'])['error_code'],'server_restarted')

    def test_metrics_separate_preview_manual_and_auto(self):
        for mode,status,started in [('mac','succeeded',True),('mac','succeeded_manual',True),('preview','blocked',False),('mac','failed',False)]:
            t=self.task(mode)
            if started:self.store.update(t['id'],'running','领取')
            self.store.update(t['id'],status,'结束')
        m=metrics(self.store.all())
        self.assertEqual(m['auto_rate'],50)
        self.assertEqual(m['auto_success'],1)
        self.assertEqual(m['preview_requests'],1)
        self.assertEqual(m['validation_rejected'],1)


class HTTPTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.app=Application(self.temp.name,preview=True)
        self.server=ThreadingHTTPServer(('127.0.0.1',0),Handler);self.server.app=self.app
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.url='http://127.0.0.1:'+str(self.server.server_port)

    def tearDown(self):
        self.server.shutdown();self.server.server_close();self.temp.cleanup()

    def request(self,path,body=None,authorized=True):
        headers={'Content-Type':'application/json'}
        if authorized:headers['Authorization']='Bearer '+self.app.token
        req=urllib.request.Request(self.url+path,data=json.dumps(body).encode() if body is not None else None,headers=headers)
        try:
            with urllib.request.urlopen(req,timeout=3) as r:return r.status,json.load(r)
        except urllib.error.HTTPError as e:
            return e.code,e.read()

    def test_private_endpoints_need_pairing(self):
        self.assertEqual(self.request('/api/state',authorized=False)[0],401)
        self.assertEqual(self.request('/.local-auth/data/secret')[0],404)
        self.assertEqual(self.request('/api/pair',{'pin':self.app.pin},False)[0],200)

    def test_idempotent_submission_and_honest_preview(self):
        body={'text':'打开腾讯会议','request_id':'one'}
        a=self.request('/api/tasks',body)[1];b=self.request('/api/tasks',body)[1]
        self.assertEqual(a['id'],b['id']);self.assertEqual(a['status'],'blocked')
        self.assertIsNone(a['started_at']);self.assertEqual(len(self.app.store.all()),1)

    def test_close_endpoint_preserves_terminal_and_requires_pairing(self):
        self.app.preview=False
        self.app.worker=Mock();self.app.worker.is_alive.return_value=True
        t=self.app.store.insert('movie', {'kind':'movie'}, 'mac', '')
        self.app.store.update(t['id'],'running','started')
        self.app.store.update(t['id'],'succeeded','played')
        route='/api/tasks/'+t['id']+'/close'
        self.assertEqual(self.request(route,{},authorized=False)[0],401)
        code,result=self.request(route,{})
        self.assertEqual(code,200)
        self.assertEqual(result['status'],'succeeded')
        self.assertEqual(result['closure']['status'],'queued')
        self.assertEqual(len(self.request(route,{})[1]['closure']['events']),1)

    def test_validation_and_immutable_terminal(self):
        t=self.request('/api/tasks',{'text':'加入会议1234'})[1]
        self.assertEqual(t['status'],'failed')
        self.assertEqual(self.request('/api/tasks/'+t['id']+'/confirm',{'success':True,'note':'伪造完成结果'})[0],409)



class CloseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.app = Application(self.temp.name, preview=True)
        self.app.preview = False
        self.app.worker = Mock()
        self.app.worker.is_alive.return_value = True
        self.executor = DesktopExecutor(self.app.store, threading.Event())

    def tearDown(self):
        self.temp.cleanup()

    def task(self, kind='movie'):
        t = self.app.store.insert('test', {'kind':kind}, 'mac', '')
        self.app.store.update(t['id'], 'running', 'started')
        result = {'meeting_id':'test-only', 'meeting_code':'123456789', 'verification':'official_api'} if kind == 'meeting_create' else {}
        return self.app.store.update(t['id'], 'succeeded', 'done', result=result)

    def test_video_close_and_idempotent_request_preserve_success(self):
        t = self.task(); page = Mock()
        page.is_closed.side_effect = [False, True]
        self.executor.pages[t['id']] = page
        self.app.request_close(t['id']); self.app.request_close(t['id'])
        self.executor.close_resource(self.app.store.get(t['id']))
        result = self.app.request_close(t['id'])
        page.close.assert_called_once()
        self.assertEqual(result['closure']['status'], 'succeeded')
        self.assertEqual(result['status'], 'succeeded')
        self.assertEqual(metrics(self.app.store.all())['auto_rate'], 100)
        self.assertEqual(metrics(self.app.store.all())['close_succeeded'], 1)

    def test_natural_close_uses_real_close_even_after_meeting_created(self):
        video = self.task()
        meeting = self.task('meeting_create')
        page = Mock()
        page.is_closed.side_effect = [False, True]
        self.executor.pages[video['id']] = page
        queued = self.app.submit('关闭', 'close-command')
        self.assertEqual(queued['id'], video['id'])
        self.assertEqual(queued['closure']['source'], 'natural_language')
        self.executor.close_resource(queued)
        page.close.assert_called_once_with(run_before_unload=False)
        self.assertEqual(self.app.store.get(video['id'])['closure']['verification'], 'page_closed')
        self.assertNotIn('closure', self.app.store.get(meeting['id']))
        self.assertEqual(len(self.app.store.all()), 2)

    def test_close_retry_never_switches_to_a_new_video(self):
        first = self.task()
        self.app.submit('关闭播放界面', 'retry-close')
        self.app.store.set_closure(first['id'], 'succeeded', 'closed')
        second = self.task()
        result = self.app.submit('关闭播放界面', 'retry-close')
        self.assertEqual(result['id'], first['id'])
        self.assertNotIn('closure', self.app.store.get(second['id']))

    def test_close_can_interrupt_waiting_video(self):
        task = self.app.store.insert('video', {'kind':'movie'}, 'mac', '')
        self.app.store.update(task['id'], 'running', 'started')
        self.app.store.update(task['id'], 'needs_input', 'waiting')
        result = self.app.submit('帮我关闭视频', 'interrupt')
        self.assertEqual(result['status'], 'cancelled')
        self.assertEqual(result['closure']['status'], 'queued')

    def test_close_without_video_never_closes_meeting(self):
        meeting = self.task('meeting_create')
        with self.assertRaisesRegex(ValueError, '没有可关闭的视频'):
            self.app.submit('关闭', 'missing')
        self.assertNotIn('closure', self.app.store.get(meeting['id']))

    def test_close_language_scope_and_preview(self):
        for text in ('关闭', '关闭播放界面', '关掉这个视频', '请帮我关闭当前视频页面。', '停止播放'):
            self.assertEqual(parse_intent(text)['kind'], 'video_close')
        for text in ('不要关闭视频', '关闭电脑', '关闭腾讯会议', '关闭后播放电影', '结束腾讯会议'):
            self.assertIn('error', parse_intent(text))
        self.app.preview = True
        task = self.app.submit('关闭', 'preview-close')
        self.assertEqual(task['status'], 'blocked')
        self.assertNotIn('closure', task)

    def test_missing_page_is_unknown_not_fake_success(self):
        t = self.task(); self.executor.close_resource(t)
        self.assertEqual(self.app.store.get(t['id'])['closure']['status'], 'unknown')

    def test_official_cancel_exact_owned_id(self):
        t = self.task('meeting_create')
        with patch('meeting_lifecycle.read_state', return_value={'ended_verified':False,'cancelled_verified':False,'detail_status':'待开始','search_status':'待开始'}), patch('meeting_cli.run', return_value={'message':'success'}) as api:
            self.executor.close_resource(t)
            self.assertEqual(api.call_args.args[0], ['meeting','cancel','--meeting-id','test-only','--meeting-type','0'])
        self.assertEqual(self.app.store.get(t['id'])['closure']['verification'], 'official_cancel_receipt')

    def test_cancel_timeout_not_retried(self):
        t = self.task('meeting_create')
        with patch('meeting_lifecycle.read_state', return_value={'ended_verified':False,'cancelled_verified':False,'detail_status':'待开始','search_status':'待开始'}), patch('meeting_cli.run', side_effect=subprocess.TimeoutExpired('tmeet',30)):
            self.executor.close_resource(t)
        self.assertEqual(self.app.request_close(t['id'])['closure']['status'], 'unknown')

    def test_manual_end_is_not_automatic_close_success(self):
        t=self.task('meeting_create')
        self.app.store.set_closure(t['id'],'succeeded','ended',manual_action=True,verification='official_ended_state')
        self.app.store.set_meeting(t['id'],'ended','official end',ended_at=1)
        m=metrics(self.app.store.all())
        self.assertEqual(m['close_succeeded'],0)
        self.assertEqual(m['meeting_ended'],1)
        self.assertEqual(m['auto_success'],1)

    def test_restart_does_not_replay_close(self):
        t=self.task();self.app.request_close(t['id'])
        restarted=Store(self.temp.name)
        self.assertEqual(restarted.get(t['id'])['closure']['status'],'unknown')

    def test_preview_and_unowned_meeting_rejected(self):
        t=self.task('meeting_open')
        with self.assertRaises(ValueError):self.app.request_close(t['id'])
        t=self.task();self.app.preview=True
        with self.assertRaises(ValueError):self.app.request_close(t['id'])

if __name__=='__main__':unittest.main(verbosity=2)
