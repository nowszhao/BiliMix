"""Compare real utterance acoustics against source-speaker reference embeddings."""
import argparse,json,math,os,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from pipeline.model_runtime import configure_diarization_environment
from pipeline.speaker_verification import utterance_spans,context_bounds,confident


def main():
    ap=argparse.ArgumentParser();ap.add_argument('audio');ap.add_argument('asr');ap.add_argument('analysis');ap.add_argument('output');args=ap.parse_args()
    analysis=json.load(open(args.analysis));asr=json.load(open(args.asr))
    if not analysis['references']:
        missing = sorted({s for w in analysis['words'] for s in
                          ([w['speaker']] if w.get('speaker') else []) + w.get('speaker_candidates', [])})
        Path(args.output).write_text(json.dumps(dict(schema_version=1, units=[], missing_reference_speakers=missing)))
        return
    model=configure_diarization_environment()
    import torch,numpy as np,soundfile as sf
    from scipy.signal import resample_poly
    from pyannote.audio import Pipeline
    torch.set_num_threads(min(6,os.cpu_count() or 1))
    pipeline=Pipeline.from_pretrained(model,token=os.environ.get('HF_TOKEN') or None)
    embedding=pipeline._embedding
    catalog={w['word_id']:w for w in analysis['words']}
    def feature(start,end):
        with sf.SoundFile(args.audio) as f:
            sr=f.samplerate;f.seek(int(start*sr));x=f.read(max(1,int((end-start)*sr)),dtype='float32',always_2d=True).mean(axis=1)
        divisor=math.gcd(sr,embedding.sample_rate)
        if sr!=embedding.sample_rate:x=resample_poly(x,embedding.sample_rate//divisor,sr//divisor)
        if len(x)<embedding.min_num_samples:return None
        with torch.inference_mode():e=np.asarray(embedding(torch.from_numpy(np.asarray(x,dtype=np.float32))[None,None,:]))[0]
        norm=np.linalg.norm(e)
        return e/norm if np.isfinite(norm) and norm>0 else None
    refs={speaker:feature(ref['start'],min(ref['end'],ref['start']+5)) for speaker,ref in analysis['references'].items()}
    expected = {w['speaker'] for w in analysis['words'] if w.get('speaker')}
    expected.update(s for w in analysis['words'] for s in w.get('speaker_candidates', []))
    refs = {s:e for s,e in refs.items() if e is not None}
    missing = sorted(expected - refs.keys())
    def scores(ids,bounds=None):
        start,end=bounds or (catalog[ids[0]]['start'],catalog[ids[-1]]['end'])
        e=feature(start,end)
        return sorted([(s,round(float(e@r),6)) for s,r in refs.items()],key=lambda p:-p[1]) if e is not None else []
    units=[]
    spans = utterance_spans(asr) if not missing else []
    def progress(completed):
        print(json.dumps(dict(event='speaker_verification', completed=completed, total=len(spans))), flush=True)
    progress(0)
    for n,ids in enumerate(spans,1):
        runs=[]
        for i in ids:
            speaker=catalog[i]['speaker']
            if not runs or runs[-1]['speaker']!=speaker:runs.append({'speaker':speaker,'word_ids':[]})
            runs[-1]['word_ids'].append(i)
        whole=scores(ids)
        for run in runs:
            run_ids = run['word_ids']
            run['scores'] = whole if len(runs)==1 else scores(run_ids)
            duration = catalog[run_ids[-1]]['end']-catalog[run_ids[0]]['start']
            if confident(run['scores'], duration) is None:
                bounds = context_bounds(analysis['words'], run_ids)
                run.update(context_scores=scores(run_ids,bounds), context_duration=bounds[1]-bounds[0])
        unit={'word_ids':ids,'scores':whole,'runs':runs}
        duration=catalog[ids[-1]]['end']-catalog[ids[0]]['start']
        if confident(whole,duration) is None:
            bounds=context_bounds(analysis['words'],ids)
            unit.update(context_scores=scores(ids,bounds),context_duration=bounds[1]-bounds[0])
        units.append(unit)
        if n%25==0 or n==len(spans):progress(n)
    Path(args.output).write_text(json.dumps({'schema_version':1,'units':units,'missing_reference_speakers':missing},indent=2))
    progress(len(spans))


if __name__=='__main__':main()
