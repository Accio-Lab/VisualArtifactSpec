"""Validate public tasks and recompute scores from existing judgments."""
import argparse
import json
import os
from pathlib import Path
from .data import read_json, sha256
from .scoring import score, normalize_judgments
from .aggregation import VERSION

def validate(root):
    catalog=read_json(root/'benchmark.json')
    seen=set(); groups=items=0
    for task in catalog['tasks']:
        task_id=task['id']
        if task_id in seen or Path(task_id).name!=task_id or task_id in {'.','..'}:
            raise ValueError('Invalid or duplicate task ID')
        seen.add(task_id);folder=root/'tasks'/task_id
        for name, expected in task.get('files',{}).items():
            if Path(name).name!=name or sha256(folder/name)!=expected:
                raise ValueError('Task file fingerprint mismatch: '+task_id+'/'+name)
        if folder.is_symlink() or not (folder/'prompt.md').read_text().strip():
            raise ValueError('Missing task prompt')
        rows=read_json(folder/'rubric.json')
        ids=[i['id'] for i in rows]
        if len(ids)!=len(set(ids)) or any(i.get('benchmark_source')!='vas' for i in rows):
            raise ValueError('Invalid unified rubric')
        judgments={i['id']:{'answer':'Yes' if set(i['answers'])=={'Yes','No'} else '0','reason':'Validation fixture'} for i in rows}
        result=score(rows,judgments)['unified']
        if abs(result['acceptance']-50)>1e-8:raise ValueError('Invalid group allocation')
        metadata=read_json(folder/'task.json')
        if metadata.get('artifact_type')!=task['artifact_type']:raise ValueError('Artifact type mismatch')
        if metadata['acceptance_groups']!={g:entry['ids'] for g,entry in result['acceptance_groups'].items()}:
            raise ValueError('Rubric/group manifest mismatch')
        for entry in read_json(folder/'inputs.manifest.json')['files']:
            p=Path(entry['path'])
            if p.is_absolute() or '..' in p.parts or len(entry['sha256'])!=64:
                raise ValueError('Invalid input reference')
        groups+=len(result['acceptance_groups']);items+=len(rows)
    if len(seen)!=catalog['task_count']:raise ValueError('Task count mismatch')
    return {'tasks':len(seen),'rubric_items':items,'acceptance_groups':groups,
            'scoring_version':VERSION,'dataset_scoring_version':catalog['scoring_version'],
            'benchmark_sha256':sha256(root/'benchmark.json')}

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    commands=parser.add_subparsers(dest='command',required=True)
    check=commands.add_parser('validate');check.add_argument('--data',type=Path,default=Path('data'))
    scoring=commands.add_parser('score')
    for name in ['rubric','judgments','output']:scoring.add_argument('--'+name,type=Path,required=True)
    scoring.add_argument('--evidence-root',type=Path)
    batch=commands.add_parser('score-all',help='Validate evidence and score existing cases')
    batch.add_argument('--data',type=Path,required=True)
    batch.add_argument('--results',type=Path,required=True)
    batch.add_argument('--output',type=Path,required=True)
    batch.add_argument('--task',action='append')
    judge=commands.add_parser('judge',help='Evaluate submissions in local Docker sandboxes')
    judge.add_argument('--data',type=Path,required=True)
    judge.add_argument('--artifacts',type=Path,required=True)
    judge.add_argument('--output',type=Path,required=True)
    judge.add_argument('--task',action='append',help='Task ID; repeat to select multiple tasks')
    judge.add_argument('--model',default=os.environ.get('JUDGE_MODEL','gpt-6-astra'))
    judge.add_argument('--reasoning',default=os.environ.get('JUDGE_REASONING_EFFORT','medium'))
    judge.add_argument('--workers',type=int,default=6)
    judge.add_argument('--image',default='vas-judge:local')
    judge.add_argument('--network',choices=['none','bridge'],default='none')
    judge.add_argument('--timeout',type=int,default=3600,help='Per-case agent budget in seconds')
    judge.add_argument('--command-timeout',type=int,default=120)
    judge.add_argument('--api-timeout',type=int,default=300)
    judge.add_argument('--max-turns',type=int,default=100)
    judge.add_argument('--max-output-tokens',type=int,default=16384)
    judge.add_argument('--resume',action='store_true',help='Reuse unchanged, validated completed cases')
    args=parser.parse_args()
    try:
        if args.command=='validate':result=validate(args.data)
        elif args.command=='score-all':
            from .batch import score_all
            validate(args.data)
            result=score_all(args.data,args.results,args.output,args.task)
        elif args.command=='judge':
            from .judge import Settings,judge_all
            validate(args.data)
            for name in ('timeout','command_timeout','api_timeout','max_turns','max_output_tokens','workers'):
                if getattr(args,name)<1:raise ValueError(name+' must be positive')
            result=judge_all(args.data,args.artifacts,args.output,
                Settings(model=args.model,reasoning=args.reasoning,image=args.image,network=args.network,
                         timeout=args.timeout,command_timeout=args.command_timeout,api_timeout=args.api_timeout,
                         max_turns=args.max_turns,max_output_tokens=args.max_output_tokens),
                task_ids=args.task,workers=args.workers,resume=args.resume)
        else:
            rows=read_json(args.rubric);judgments=read_json(args.judgments)
            if args.output.resolve() in {args.rubric.resolve(),args.judgments.resolve()}:
                raise ValueError('Output must not overwrite rubric or judgments')
            if args.evidence_root:
                from .evidence import snapshot
                evidence_judgments=read_json(args.evidence_root/'evaluation/result.json')
                if normalize_judgments(evidence_judgments)!=normalize_judgments(judgments):
                    raise ValueError('Evidence judgments do not match requested judgments')
                snapshot(args.evidence_root,rows)
            result=score(rows,judgments)
            args.output.parent.mkdir(parents=True,exist_ok=True)
            args.output.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
        print(json.dumps(result,ensure_ascii=False,indent=2))
        if args.command in {'score-all','judge'} and (result['failed'] or result['missing']):return 1
        return 0
    except (OSError,ValueError,KeyError,TypeError,RuntimeError) as error:
        parser.exit(2,str(error)+'\n')
