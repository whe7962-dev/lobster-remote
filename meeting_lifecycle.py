"""只读官方会议状态和用户主动触发的客户端入口；不模拟点击或执行 AppleScript。"""
import platform
import re
import secrets
import subprocess
import time
import os

import meeting_cli


def scheme(code):
    if not re.fullmatch(r'\d{9,10}', str(code)):
        raise ValueError('会议号格式错误')
    # 与腾讯官方邀请网页 getSchemaUrl 的基本参数一致。
    return 'wemeet://page/inmeeting?meeting_code=' + str(code) + '&token=&launch_id=' + secrets.token_hex(8) + '&ip_oversea=0'


def read_state(result):
    meeting_id = str(result['meeting_id'])
    info = meeting_cli.meeting_info(meeting_cli.run(['meeting', 'get', '--meeting-id', meeting_id], timeout=15))
    if not info or str(info['meeting_id']) != meeting_id:
        raise ValueError('没有取得该会议的官方详情')
    found = meeting_cli.run(['meeting', 'search', '--meeting-code', str(result['meeting_code'])], timeout=15)
    matches = [o for o in meeting_cli.objects(found) if str(o.get('meeting_id', '')) == meeting_id and 'status' in o]
    search_status = matches[0]['status'] if matches else None
    raw_status = info.get('status')
    cancelled = raw_status in ('已取消', 'MEETING_STATE_CANCELLED')
    # “无状态”“待开始”“已删除”均不能作为结束一场进行中会议的证据。
    ongoing = raw_status in ('会议中', 'MEETING_STATE_STARTED') or search_status in ('进行中', 'ongoing')
    ended = not cancelled and not ongoing and search_status in ('已结束', 'closed')
    evidence = dict(detail_status=raw_status, search_status=search_status, observed_at=time.time(), verification='official_meeting_state', joined_verified=False, ended_verified=ended, cancelled_verified=cancelled)
    if ended or cancelled:
        return evidence
    # 仅返回当前账号在此会议的本机参会证据；不保存原始参会者手机号、IP、位置等数据。
    hosts = {str(h.get('operator_id')) for h in info.get('current_hosts', []) if h.get('operator_id')}
    report = meeting_cli.run(['report', 'participants', '--meeting-id', meeting_id], timeout=15)
    expected_instance = 'Mac' if platform.system() == 'Darwin' else 'Windows'
    own = []
    for p in report.get('data', {}).get('participants', []):
        is_creator = '创建者' in str(p.get('user_role', ''))
        is_host = str(p.get('open_id', '')) in hosts
        local_device = str(p.get('instanceid')) in ({'Mac','2'} if expected_instance == 'Mac' else {'Windows','PC','1'})
        if is_creator and is_host and local_device and p.get('join_time') and not p.get('left_time'):
            own.append(p)
    if ongoing and own:
        p = own[-1]
        evidence.update(joined_verified=True, verification='official_participant_and_meeting_state', join_time=p.get('join_time'), instance=expected_instance, microphone_on=p.get('audio_state'), camera_on=p.get('video_state'))
    return evidence


class Lifecycle:
    def __init__(self, executor):
        self.executor = executor
        self.store = executor.store
        self.watches = {}

    def action(self, task, action):
        task_id = task['id']
        try:
            evidence = read_state(task['result'])
            if evidence['ended_verified'] or evidence['cancelled_verified']:
                return self.record(task, evidence)
            if action == 'meeting-join':
                if not evidence['joined_verified']:
                    url = scheme(task['result']['meeting_code'])
                    if platform.system() == 'Darwin':
                        proc = subprocess.run(['/usr/bin/open', '-a', self.executor.meeting_path(), url], capture_output=True, timeout=12)
                        if proc.returncode:
                            raise RuntimeError('系统未能打开此会议的入会入口')
                    elif platform.system() == 'Windows':
                        os.startfile(url)
                    else:
                        raise RuntimeError('当前系统不支持真实客户端入会')
                    self.store.set_meeting(task_id, 'awaiting_join', '已打开此会议的客户端入口。请在电脑完成必要的入会提示；系统会查询官方参会记录，打开窗口本身不算入会。', evidence=evidence)
                else:
                    self.record(task, evidence)
            elif action == 'meeting-end':
                self.record(task, evidence)
                self.store.set_closure(task_id, 'needs_input', '请在电脑腾讯会议点击“结束会议”→“全员结束会议”。只离开或关闭窗口不算结束；正在查询官方结束状态。', action='end_meeting', manual_action=True)
                path = self.executor.meeting_path()
                if path:
                    if platform.system() == 'Darwin':
                        subprocess.run(['/usr/bin/open', '-a', path], capture_output=True, timeout=12)
                    elif platform.system() == 'Windows':
                        subprocess.Popen([path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            else:
                self.record(task, evidence)
            self.watches[task_id] = {'next': time.time()+5, 'until': time.time()+180}
        except Exception as exc:
            self.store.set_meeting(task_id, 'unknown', '官方状态暂时无法确认：' + str(exc)[:200] + '。可稍后点击“检查会议状态”；不会重复创建。')

    def record(self, task, evidence):
        task_id = task['id']
        current = self.store.get(task_id)
        if evidence['cancelled_verified']:
            self.store.set_meeting(task_id, 'cancelled', '官方状态：预约已取消。这不等于结束一场进行中的会议。', evidence=evidence)
            self.watches.pop(task_id, None)
        elif evidence['ended_verified']:
            self.store.set_meeting(task_id, 'ended', '腾讯官方状态确认：本场会议已结束。客户端操作由人工完成，系统自动核验。', evidence=evidence, ended_at=current.get('meeting', {}).get('ended_at') or time.time())
            self.store.set_closure(task_id, 'succeeded', '会议已结束：依据官方会议搜索状态“已结束”；客户端人工操作，非自动取消预约。', action='end_meeting', manual_action=True, verification='official_ended_state', observed_at=time.time())
            self.watches.pop(task_id, None)
        elif evidence['joined_verified']:
            self.store.set_meeting(task_id, 'joined', '已核验本机账号在此会议中（腾讯官方参会记录）。', evidence=evidence, joined_at=current.get('meeting', {}).get('joined_at') or time.time())
        else:
            self.store.set_meeting(task_id, 'awaiting_join', '会议已创建，尚未核验本机入会。请在电脑加入这场会议，再检查状态。', evidence=evidence)

    def poll(self):
        now = time.time()
        for task_id, watch in list(self.watches.items()):
            if now > watch['until']:
                self.watches.pop(task_id, None)
                task = self.store.get(task_id)
                if task.get('closure', {}).get('status') == 'needs_input':
                    self.store.set_closure(task_id, 'needs_input', '自动检查已暂停。请在客户端完成“全员结束会议”，然后点击“检查会议状态”。', manual_action=True)
                continue
            if now < watch['next']:
                continue
            watch['next'] = now + 8
            task = self.store.get(task_id)
            try:
                self.record(task, read_state(task['result']))
            except Exception:
                self.store.set_meeting(task_id, 'unknown', '官方状态查询暂时失败，未确认入会或结束。自动检查稍后重试。')
            break  # 每轮最多查询一场，避免阻塞其他任务。
