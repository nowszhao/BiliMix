"""Compare real utterance acoustics against source-speaker reference embeddings."""
import argparse,json,math,os,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from pipeline.model_runtime import configure_diarization_environment
from pipeline.speaker_verification import utterance_spans,context_bounds,confident


def main():
    ap=argparse.ArgumentParser();ap.add_argument('audio');ap.add_argument('asr');ap.add_argument('analysis');ap.add_argument('output');args=ap.parse_args()
    model=configure_diarization_environment()
    import torch,numpy as np,soundfile as sf
    from scipy.signal import resample_poly
    from pyannote.audio import Pipeline
    torch.set_num_threads(min(6,os.cpu_count() or 1))
    pipeline=Pipeline.from_pretrained(model,token=os.environ.get('HF_TOKEN') or None)
    embedding=pipeline._embedding
    analysis=json.load(open(args.analysis));asr=json.load(open(args.asr))
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
    if any(e is None for e in refs.values()):raise ValueError('Unusable speaker reference embedding')
    def scores(ids,bounds=None):
        start,end=bounds or (catalog[ids[0]]['start'],catalog[ids[-1]]['end'])
        e=feature(start,end)
        return sorted([(s,round(float(e@r),6)) for s,r in refs.items()],key=lambda p:-p[1]) if e is not None else []
    units=[]
    for n,ids in enumerate(utterance_spans(asr),1):
        runs=[]
        for i in ids:
            speaker=catalog[i]['speaker']
            if not runs or runs[-1]['speaker']!=speaker:runs.append({'speaker':speaker,'word_ids':[]})
            runs[-1]['word_ids'].append(i)
        whole=scores(ids)
        for run in runs:run['scores']=whole if len(runs)==1 else scores(run['word_ids'])
        unit={'word_ids':ids,'scores':whole,'runs':runs}
        duration=catalog[ids[-1]]['end']-catalog[ids[0]]['start']
        if confident(whole,duration) is None:
            bounds=context_bounds(analysis['words'],ids)
            unit.update(context_scores=scores(ids,bounds),context_duration=bounds[1]-bounds[0])
        units.append(unit)
        if n%25==0:print(f'Acoustic verification {n}',flush=True)
    Path(args.output).write_text(json.dumps({'schema_version':1,'units':units},indent=2))
    print(f'Acoustic verification complete: {len(units)} windows',flush=True)


if __name__=='__main__':main()
