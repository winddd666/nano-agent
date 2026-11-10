"""Task prompts must change without losing command tools or changing coding defaults."""
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from agent import run_agent
from test_agent import assistant, request


class QATests(unittest.TestCase):
    def setUp(self):
        output = redirect_stdout(io.StringIO())
        output.__enter__()
        self.addCleanup(output.__exit__, None, None, None)
        work = tempfile.TemporaryDirectory()
        self.addCleanup(work.cleanup)
        self.work = Path(work.name)

    def test_qa_keeps_tools_and_uses_qa_reviewer_without_requiring_commands(self):
        schemas = []

        def actor(messages, tools):
            schemas.append(tools)
            system = messages[0]['content']
            self.assertIn('通用问答助手', system)
            self.assertNotIn('先读代码和相关测试', system)
            self.assertNotIn('最终回答前查看 get_diff', system)
            return assistant(content='Final Answer: example')

        def reviewer(messages, tools):
            self.assertIn('通用问答任务的独立评审者', messages[0]['content'])
            self.assertNotIn('代码任务的独立评审者', messages[0]['content'])
            data = json.loads(messages[1]['content'])
            self.assertEqual(data['task'], 'A generic question')
            return assistant(content=json.dumps({'recommendation': 'accept', 'reason': '符合问题要求'}))

        with patch('agent.chat', side_effect=actor), patch('reviewer.chat', side_effect=reviewer):
            report = run_agent('A generic question', profile='coding', task_mode='qa', workspace=self.work)
        self.assertEqual(report['status'], 'answered')
        self.assertEqual(report['task_mode'], 'qa')
        self.assertEqual(report['reviews'][0]['recommendation'], 'accept')
        with patch('agent.chat', return_value=assistant(content='Done')) as model:
            run_agent('Code task', profile='coding', workspace=self.work, review=False)
        self.assertEqual(schemas[0], model.call_args.kwargs['tools'])
        self.assertIn('先读代码和相关测试', model.call_args.args[0][0]['content'])

    def test_qa_can_really_execute_python(self):
        def actor(messages, tools):
            results = [m for m in messages if m['role'] == 'tool']
            if not results:
                return assistant(request('compute', 'run_command', {
                    'argv': ['python3', '-c', 'print(2 + 3)'], 'cwd': '.', 'env': {}, 'mode': 'foreground'}))
            result = json.loads(results[-1]['content'])
            self.assertEqual(result['exit_code'], 0)
            self.assertEqual(result['output'].strip(), '5')
            return assistant(content='Final Answer: 5')

        with patch('agent.chat', side_effect=actor):
            report = run_agent('Calculate 2+3', profile='coding', task_mode='qa', workspace=self.work,
                               review=False, max_calls=3)
        self.assertEqual(report['status'], 'answered')


if __name__ == '__main__':
    unittest.main()
