"""CPU accounting must not confuse wall time, machine percentage or streams."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]/'scripts'))
from performance_cpu import ACTIVE, CpuApplication, cpu_delta, new_row, sql_probe


class CpuProbeTests(unittest.TestCase):
    def test_cpu_percentage_uses_full_window_and_distinguishes_one_core(self):
        with patch('performance_cpu.os.cpu_count', return_value=16):
            result=cpu_delta({'identity':1,'cpu_ms':100,'clock':2}, {'identity':1,'cpu_ms':600,'clock':3})
        self.assertEqual(result['process_cpu_ms'],500)
        self.assertEqual(result['window_wall_ms'],1000)
        self.assertEqual(result['one_core_percent'],50)
        self.assertEqual(result['machine_percent'],3.125)
        with self.assertRaisesRegex(RuntimeError,'pid_reused'):
            cpu_delta({'identity':1}, {'identity':2})

    def test_invalid_clock_and_cpu_deltas_are_not_hidden(self):
        before={'identity':1,'cpu_ms':100,'clock':2}
        for after in ({'identity':1,'cpu_ms':110,'clock':2}, {'identity':1,'cpu_ms':90,'clock':3}):
            with self.assertRaisesRegex(RuntimeError,'invalid_cpu_interval'):
                cpu_delta(before, after)

    def test_sql_write_never_reaches_database_and_sql_text_is_not_logged(self):
        execute=Mock(return_value='ok')
        with self.assertRaisesRegex(RuntimeError,'write_blocked'):
            sql_probe(execute,'UPDATE example SET secret=1',[],False,{})
        execute.assert_not_called()
        row=new_row()
        token=ACTIVE.set(row)
        try:
            self.assertEqual(sql_probe(execute,'SELECT 1',[],False,{}),'ok')
        finally:
            ACTIVE.reset(token)
        self.assertEqual(row['sql_count'],1)
        self.assertGreaterEqual(row['sql_ms'],0)
        self.assertNotIn('SELECT',json.dumps(row))

    def test_stream_is_consumed_and_closed_before_cpu_record_is_written(self):
        class Body:
            closed=False
            def __iter__(self):
                self.assert_scope=ACTIVE.get() is not None
                yield b'image'
            def close(self):
                self.closed=True
        body=Body()
        def app(environ,start_response):
            start_response('200 OK',[('X-Request-ID','probe-id')])
            return body
        with tempfile.TemporaryDirectory() as directory, patch('django.db.connections',{}):
            target=Path(directory)/'requests.jsonl'
            result=CpuApplication(app,target)({'REQUEST_METHOD':'GET','PATH_INFO':'/api/v1/snapshots/private-id/thumbnail'},Mock())
            self.assertFalse(target.exists())
            self.assertEqual(b''.join(result),b'image')
            row=json.loads(target.read_text(encoding='utf-8'))
            self.assertTrue(body.closed)
            self.assertTrue(body.assert_scope)
            self.assertEqual(row['status'],200)
            self.assertEqual(row['route'],'/api/v1/snapshots/:id/thumbnail')
            self.assertNotIn('private-id',target.read_text(encoding='utf-8'))
            self.assertGreaterEqual(row['app_wall_ms'],row['construct_ms'])
            self.assertIsNone(ACTIVE.get())

    def test_failed_application_restores_probe_scope(self):
        with tempfile.TemporaryDirectory() as directory, patch('django.db.connections',{}):
            app=CpuApplication(Mock(side_effect=ValueError('test')),Path(directory)/'requests.jsonl')
            with self.assertRaises(ValueError):
                app({'REQUEST_METHOD':'GET'},Mock())
            self.assertIsNone(ACTIVE.get())


if __name__=='__main__':
    unittest.main()
