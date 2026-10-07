import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('wait_reply', ROOT / 'scripts/wait_reply.py')
w = importlib.util.module_from_spec(spec)
spec.loader.exec_module(w)


def message(content='answer', mid='om_r', **extra):
    return dict(message_id=mid, reply_to='om_q', content=content,
                sender={'sender_type': 'app', 'id': 'bot'}, **extra)


class ReplyTests(unittest.TestCase):
    def test_single_and_correlation(self):
        r = w.Replies('om_q', 'bot')
        wrong = message(mid='wrong'); wrong['reply_to'] = 'om_other'
        human = message(mid='human'); human['sender']['sender_type'] = 'user'
        bot = message(mid='otherbot'); bot['sender']['id'] = 'other'
        r.add([wrong, human, bot, message()])
        self.assertEqual(r.result()['content'], 'answer')

    def test_duplicate_is_not_completion(self):
        r = w.Replies('om_q')
        r.add([message('a (1/2)', 'one'), message('a (1/2)', 'duplicate')])
        self.assertIsNone(r.result())
        r.add([message('b (2/2)', 'two')])
        self.assertEqual(r.result()['content'], 'a (1/2)\nb (2/2)')

    def test_out_of_order_and_progress(self):
        r = w.Replies('om_q')
        r.add([message('b (2/2)', 'two'), message('working', 'progress')])
        self.assertIsNone(r.result())
        r.add([message('a (1/2)', 'one')])
        self.assertEqual([m['message_id'] for m in r.result()['replies']], ['one', 'two'])

    def test_invalid_parts(self):
        for contents in [['a (0/2)'], ['a (3/2)'], ['a (1/2)', 'b (2/3)'],
                         ['a (1/2)', 'different (1/2)']]:
            with self.subTest(contents=contents):
                r = w.Replies('om_q')
                r.add([message(c, str(i)) for i, c in enumerate(contents)])
                with self.assertRaises(w.ReplyError):
                    r.result()

    def test_nested_thread_is_scoped(self):
        r = w.Replies('om_q')
        child = message(); child.pop('reply_to'); child['thread_id'] = 'omt_t'
        r.add([{'message_id': 'other', 'thread_replies': [message('wrong')]},
               {'message_id': 'om_q', 'thread_id': 'omt_t', 'thread_replies': [child]}])
        self.assertEqual(r.result()['content'], 'answer')
        self.assertEqual(r.threads, {'omt_t'})

    def test_page_errors(self):
        for payload in [{'code': 123}, {}, {'data': {'messages': None}}]:
            with self.assertRaises(w.ReplyError):
                w.page_data(payload)

    def test_malformed_sender_is_controlled_error(self):
        r = w.Replies('om_q')
        msg = message(); msg['sender'] = 'invalid'
        with self.assertRaises(w.ReplyError):
            r.add([msg])

    def test_deleted_reply_does_not_remain_complete(self):
        r = w.Replies('om_q')
        r.add([message('a (1/2)', 'one')])
        r.add([message('a (1/2)', 'one', deleted=True), message('b (2/2)', 'two')])
        self.assertIsNone(r.result())

    def test_pagination(self):
        responses = [{'messages': [], 'has_more': True, 'page_token': 'next'},
                     {'messages': [message()], 'has_more': False}]
        with patch.object(w, 'run_json', side_effect=responses) as run:
            self.assertEqual(len(list(w.pages(['cli'], time.monotonic()+10))), 2)
            self.assertEqual(run.call_args_list[1].args[0], ['cli', '--page-token', 'next'])

    def test_repeated_token(self):
        data = {'messages': [], 'has_more': True, 'page_token': 'same'}
        with patch.object(w, 'run_json', return_value=data), self.assertRaises(w.ReplyError):
            list(w.pages(['cli'], time.monotonic()+10))

    def test_poll_finds_second_page(self):
        args = SimpleNamespace(start=None, chat_id='oc_c', interval=.01)
        responses = [{'messages': [], 'has_more': True, 'page_token': 'next'},
                     {'messages': [message()], 'has_more': False}]
        with patch.object(w, 'run_json', side_effect=responses):
            self.assertEqual(w.poll(args, w.Replies('om_q'), time.monotonic()+2)['content'], 'answer')

    def test_thread_fetch_beyond_nested_limit(self):
        args = SimpleNamespace(start=None, chat_id='oc_c', interval=.01)
        responses = [{'messages': [{'message_id': 'om_q', 'thread_id': 'omt_t',
                                    'thread_replies': [message('a (1/2)', 'one')]}]},
                     {'messages': [message('b (2/2)', 'two')]}]
        with patch.object(w, 'run_json', side_effect=responses) as run:
            result = w.poll(args, w.Replies('om_q'), time.monotonic()+2)
            self.assertIn('b (2/2)', result['content'])
            self.assertIn('+threads-messages-list', run.call_args.args[0])

    def test_errors_do_not_look_like_no_reply(self):
        args = SimpleNamespace(start=None, chat_id='oc_c', interval=.001)
        with patch.object(w, 'run_json', side_effect=w.ReplyError('failed')), self.assertRaises(w.ReplyError):
            w.poll(args, w.Replies('om_q'), time.monotonic()+2)

    def test_ocs_saved_first_and_not_retried(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp)/'result.json'
            def notify(command, **kwargs):
                self.assertTrue(output.exists())
                self.assertEqual(command[:3], ['ocs', 'dm', 'codex-test'])
                return SimpleNamespace(returncode=3)
            with patch.object(w.shutil, 'which', return_value='/mock/ocs'), \
                    patch.object(w, 'stream', return_value={'content': 'ok'}), \
                    patch.object(w.subprocess, 'run', side_effect=notify) as run:
                self.assertEqual(w.main(['oc_c', 'om_q', '--source', 'stdin',
                                         '--output', str(output), '--notify', 'codex-test']), 5)
                self.assertEqual(run.call_count, 1)

    def invoke(self, data, *args):
        return subprocess.run([sys.executable, str(ROOT/'scripts/wait_reply.py'),
                               'oc_c', 'om_q', '--source', 'stdin', *args],
                              input=data, text=True, capture_output=True, timeout=3)

    def test_push_cli_output_and_permissions(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp)/'result.json'
            data = json.dumps({'data': {'messages': [message('答案')]}})+'\n'
            run = self.invoke(data, '--output', str(output))
            self.assertEqual(run.returncode, 0, run.stderr)
            self.assertEqual(json.loads(output.read_text())['content'], '答案')
            self.assertEqual(output.stat().st_mode & 0o777, 0o600)
            again = self.invoke(data, '--output', str(output))
            self.assertEqual(again.returncode, 3)

    def test_push_closed_or_malformed(self):
        for data in ['', 'not json\n', '{"code":1}\n']:
            self.assertEqual(self.invoke(data).returncode, 4)

    def test_timeout_with_open_stream(self):
        proc = subprocess.Popen([sys.executable, str(ROOT/'scripts/wait_reply.py'),
                                 'oc_c', 'om_q', '0.05', '--source', 'stdin'],
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            self.assertEqual(proc.wait(timeout=2), 2)
        finally:
            proc.communicate()

    def test_argument_validation(self):
        for value in ['0', '-1', 'nan', 'inf']:
            self.assertNotEqual(self.invoke('', value).returncode, 0)
        self.assertEqual(self.invoke('', '--start', 'yesterday').returncode, 2)


if __name__ == '__main__':
    unittest.main()
