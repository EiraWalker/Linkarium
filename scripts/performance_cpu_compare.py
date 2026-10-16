"""Alternate trusted Git baseline/current GET handlers in one isolated process."""
import argparse
from contextlib import ExitStack
import hashlib
import json
import os
from pathlib import Path
import statistics
import subprocess
import sys
import time
import types
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'scripts'),str(ROOT/'backend')]
os.environ.setdefault('BOOKMARK_DATA_ROOT',str(ROOT/'data'))
os.environ.setdefault('DJANGO_SETTINGS_MODULE','config.settings')
os.environ['BOOKMARK_PERF']='0'
from performance_cpu import ACTIVE, new_row, now, sql_probe
from performance_browser import temporary_owner_session


def baseline_modules(revision):
    modules={}
    hashes={}
    for package, name in [('library','catalog'),('library','queue'),('library','preview_cache'),('config','api')]:
        path=f'backend/{package}/{name}.py'
        source=subprocess.check_output(['git','show',f'{revision}:{path}'],cwd=ROOT)
        hashes[path]=hashlib.sha256(source).hexdigest()
        module=types.ModuleType(f'{package}._performance_baseline_{name}')
        module.__package__=package
        module.__file__=str(ROOT/path)
        exec(compile(source,module.__file__,'exec'),module.__dict__)
        modules[name]=module
    modules['api'].catalog=modules['catalog']
    modules['api'].queue=modules['queue']
    return modules,hashes


def compare(args):
    import django
    django.setup()
    from django.db import connections
    from django.test import Client
    from config import api
    from library import preview_cache
    from library.models import SnapshotVersion
    revision=subprocess.check_output(['git','rev-parse','--verify',args.baseline_ref+'^{commit}'],cwd=ROOT,text=True).strip()
    baseline,hashes=baseline_modules(revision)
    directory=args.output.resolve()
    directory.mkdir(parents=True,exist_ok=False)
    result={'started_at':now(),'baseline_ref':args.baseline_ref,'baseline_commit':revision,'baseline_source_sha256':hashes,
            'scope':'Single Python process, same session/client/files, alternating GET handlers; no HTTP/browser/profiler.',
            'samples':[],'status':'incomplete'}
    state={'deleted':False}
    try:
        with temporary_owner_session('http://127.0.0.1') as (cookie,state):
            client=Client()
            client.cookies[cookie['name']]=cookie['value']
            paths={}
            for device in ('desktop','mobile'):
                response=client.get('/api/v1/bookmarks?variant='+device+'&page_size=48')
                response.close()
                ids=[row['preview']['version_id'] for row in response.json()['data'] if row.get('preview')]
                versions={str(v.id):v for v in SnapshotVersion.objects.filter(pk__in=ids)}
                paths[device]=[f'/api/v1/snapshots/{identity}/thumbnail?v=1' for identity in ids
                               if preview_cache.image_path(versions[identity]).is_file()]
                if len(paths[device])<10:
                    raise RuntimeError('insufficient_existing_images')
            etags={}
            sizes={}
            def fetch(target,conditional=False):
                response=client.get(target,**({'HTTP_IF_NONE_MATCH':etags[target]} if conditional else {}))
                expected=304 if conditional else 200
                if response.status_code!=expected:
                    raise RuntimeError('unexpected_status_'+str(response.status_code))
                body=b''.join(response.streaming_content) if response.streaming else response.content
                if response.has_header('ETag'):
                    tag=response['ETag']
                    if target in etags and etags[target]!=tag:
                        raise RuntimeError('preview_changed_during_comparison')
                    etags[target]=tag
                    if not conditional:
                        digest=hashlib.sha256(body).hexdigest()
                        if target in sizes and sizes[target]!=digest:
                            raise RuntimeError('preview_content_mismatch')
                        sizes[target]=digest
                response.close()
            for targets in paths.values():
                for target in targets:
                    fetch(target)
            cases=[('bookmarks',['/api/v1/bookmarks?variant=desktop&page_size=48'],20,False),
                   ('folders',['/api/v1/folders'],30,False),
                   ('job_groups',['/api/v1/job-groups?page=1&page_size=20'],20,False)]
            for device,targets in paths.items():
                cases.extend([(device+'_200',targets,90,False),(device+'_304',targets,90,True)])
            for repeat in range(3):
                for label,targets,count,conditional in cases:
                    for condition in (('before','after') if repeat%2==0 else ('after','before')):
                        with ExitStack() as stack:
                            if condition=='before':
                                stack.enter_context(patch.object(api,'route_request',baseline['api'].route_request))
                                stack.enter_context(patch.object(preview_cache,'preview_response',baseline['preview_cache'].preview_response))
                            for alias in connections:
                                stack.enter_context(connections[alias].execute_wrapper(sql_probe))
                            for target in targets[:2]:
                                fetch(target,conditional)
                            row=new_row()
                            token=ACTIVE.set(row)
                            try:
                                cpu,wall=time.process_time_ns(),time.perf_counter_ns()
                                for i in range(count):
                                    fetch(targets[i%len(targets)],conditional)
                                row.update(cpu_ms=(time.process_time_ns()-cpu)/1e6,
                                           wall_ms=(time.perf_counter_ns()-wall)/1e6)
                            finally:
                                ACTIVE.reset(token)
                        row.update(label=label,repeat=repeat+1,condition=condition,count=count)
                        result['samples'].append(row)
                        print(json.dumps({k:row[k] for k in ('label','repeat','condition','count','cpu_ms','wall_ms')}),flush=True)
        result['comparison']=[]
        for label,*_ in cases:
            row={'label':label}
            for condition in ('before','after'):
                samples=[r for r in result['samples'] if r['label']==label and r['condition']==condition]
                row[condition]={'cpu_per_request_ms':statistics.median(r['cpu_ms']/r['count'] for r in samples),
                                'wall_per_request_ms':statistics.median(r['wall_ms']/r['count'] for r in samples),
                                'samples':len(samples)}
            row['cpu_reduction_percent']=100*(1-row['after']['cpu_per_request_ms']/row['before']['cpu_per_request_ms'])
            result['comparison'].append(row)
        result['status']='complete'
    finally:
        result.update(temporary_session_deleted=state['deleted'],finished_at=now())
        (directory/'comparison.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline-ref',required=True)
    parser.add_argument('--output',type=Path,required=True)
    compare(parser.parse_args())


if __name__=='__main__':
    main()
