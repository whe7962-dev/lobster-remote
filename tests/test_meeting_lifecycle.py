import sys
import unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from meeting_lifecycle import read_state, scheme
from app import parse_intent

class MeetingStateTests(unittest.TestCase):
    result={'meeting_id':'1','meeting_code':'123456789'}
    def response(self,status,search,participants=None):
        info={'meeting_id':'1','meeting_code':'123456789','status':status,'current_hosts':[{'operator_id':'owner'}]}
        return [{'data':{'meeting_info_list':[info]}},{'data':{'meetings':[{'meeting_id':'1','status':search}]}},{'data':{'participants':participants or []}}]
    def test_schedule_or_no_state_never_means_ended(self):
        for status in ['待开始','无状态','已删除']:
            with patch('meeting_cli.run',side_effect=self.response(status,'待开始')):
                self.assertFalse(read_state(self.result)['ended_verified'])
    def test_cancel_is_not_end(self):
        with patch('meeting_cli.run',side_effect=self.response('已取消','已结束')):
            r=read_state(self.result)
            self.assertFalse(r['ended_verified']);self.assertTrue(r['cancelled_verified'])
    def test_end_requires_official_closed_search(self):
        with patch('meeting_cli.run',side_effect=self.response('待开始','已结束')):
            self.assertTrue(read_state(self.result)['ended_verified'])
    def test_stale_search_closed_cannot_override_live_meeting(self):
        with patch('meeting_cli.run',side_effect=self.response('会议中','已结束')):
            self.assertFalse(read_state(self.result)['ended_verified'])

    def test_other_participant_is_not_self_joined(self):
        p={'open_id':'someone_else','user_role':'成员','instanceid':'Mac','join_time':'now','left_time':''}
        with patch('platform.system',return_value='Darwin'),patch('meeting_cli.run',side_effect=self.response('会议中','进行中',[p])):
            self.assertFalse(read_state(self.result)['joined_verified'])
    def test_self_join_proof_omits_private_fields(self):
        p={'open_id':'owner','user_role':'创建者+主持人','instanceid':'Mac','join_time':'now','left_time':'','ip':'secret','phone':'secret','audio_state':False,'video_state':False}
        with patch('platform.system',return_value='Darwin'),patch('meeting_cli.run',side_effect=self.response('会议中','进行中',[p])):
            r=read_state(self.result);self.assertTrue(r['joined_verified'])
            self.assertNotIn('secret',str(r));self.assertNotIn('owner',str(r))
    def test_left_member_is_not_currently_joined(self):
        p={'open_id':'owner','user_role':'创建者+主持人','instanceid':'Mac','join_time':'before','left_time':'now'}
        with patch('platform.system',return_value='Darwin'),patch('meeting_cli.run',side_effect=self.response('会议中','进行中',[p])):
            self.assertFalse(read_state(self.result)['joined_verified'])
    def test_closing_text_cannot_start_movie_search(self):
        self.assertEqual(parse_intent('关闭播放页面')['kind'],'video_close')
        self.assertEqual(parse_intent('结束腾讯会议')['code'],'use_task_action')
    def test_scheme_input_allowlist(self):
        self.assertTrue(scheme('123456789').startswith('wemeet://page/inmeeting?'))
        with self.assertRaises(ValueError):scheme('123456789&camera=1')

if __name__=='__main__':unittest.main()
