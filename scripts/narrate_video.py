"""Add synchronized synthetic Chinese narration to the verified browser video.

Only public narration text is sent to Edge TTS. No API keys, task inputs or
video frames are sent. The original silent video is preserved.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import wave

ROOT=Path(__file__).resolve().parents[1]


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--proxy",default=os.environ.get("NARRATION_PROXY"))
    args=parser.parse_args()
    import edge_tts
    import imageio_ffmpeg
    from PIL import Image, ImageDraw, ImageFont
    ffmpeg=imageio_ffmpeg.get_ffmpeg_exe()
    source=ROOT/"deliverables"/"AnimFlow-demo.mp4"
    if not source.exists():source=ROOT/"docs"/"assets"/"AnimFlow-demo.mp4"
    spec=json.loads((ROOT/"docs"/"narration.json").read_text(encoding="utf-8"))
    work=ROOT/"data"/"narration"
    work.mkdir(parents=True,exist_ok=True)
    def duration(path):
        r=subprocess.run([ffmpeg,"-hide_banner","-i",str(path)],capture_output=True,text=True)
        m=re.search(r"Duration: (\d+):(\d+):(\d+\.\d+)",r.stderr)
        if not m:raise RuntimeError("Unable to read duration: "+str(path))
        h,mi,s=map(float,m.groups())
        return h*3600+mi*60+s
    seconds=duration(source)
    assert seconds>205.7, "Narration timeline expects the verified 206-second recording"
    async def synthesize():
        semaphore=asyncio.Semaphore(3)
        async def one(index,segment):
            fingerprint=hashlib.sha256((spec['voice']+spec['rate']+segment['text']).encode()).hexdigest()[:12]
            path=work/f"segment-{index:02d}-{fingerprint}.mp3"
            segment['audio_path']=str(path)
            if path.exists() and path.stat().st_size>1000:return
            async with semaphore:
                speech=edge_tts.Communicate(segment['text'],spec['voice'],rate=spec['rate'],
                                            proxy=args.proxy,connect_timeout=10,receive_timeout=30)
                await speech.save(str(path))
                print(f"Synthesized segment {index+1}/{len(spec['segments'])}",flush=True)
        await asyncio.gather(*(one(i,s) for i,s in enumerate(spec['segments'])))
    asyncio.run(synthesize())
    frames=int(round(seconds*48000))
    track=bytearray(frames*2)
    report=[]
    for index,seg in enumerate(spec['segments']):
        raw_duration=duration(seg['audio_path'])
        available=seg['end']-seg['start']-.20
        tempo=max(1.0,raw_duration/available)
        if tempo>1.25:
            raise RuntimeError(f"Segment {index+1} too dense: {raw_duration:.2f}s vs {available:.2f}s. Shorten text.")
        wav=work/f"aligned-{index:02d}.wav"
        r=subprocess.run([ffmpeg,"-y","-v","error","-i",seg['audio_path'],"-af",
                          f"atempo={tempo:.6f},loudnorm=I=-17:TP=-2:LRA=7,aresample=48000",
                          "-ac","1","-c:a","pcm_s16le",str(wav)],capture_output=True,text=True)
        if r.returncode:raise RuntimeError(r.stderr)
        with wave.open(str(wav),'rb') as audio:
            assert audio.getframerate()==48000 and audio.getsampwidth()==2 and audio.getnchannels()==1
            samples=audio.readframes(audio.getnframes())
        start=int(round(seg['start']*48000))*2
        finish=start+len(samples)
        actual_duration=len(samples)/2/48000
        assert actual_duration<=seg['end']-seg['start']-.05,(index,actual_duration)
        assert finish<=len(track)
        track[start:finish]=samples
        entry={'start':seg['start'],'end':seg['end'],'speech_seconds':round(actual_duration,3),
               'tempo':round(tempo,3),'text':seg['text']}
        report.append(entry)
        print(f"Aligned {index+1}: {actual_duration:.2f}s in {seg['end']-seg['start']:.2f}s, tempo={tempo:.3f}",flush=True)
    narration=work/"narration.wav"
    with wave.open(str(narration),'wb') as wav:
        wav.setnchannels(1);wav.setsampwidth(2);wav.setframerate(48000);wav.writeframes(track)

    # Correct the silent edition's opening/closing label without changing task visuals.
    font_path='C:/Windows/Fonts/msyh.ttc'
    font=ImageFont.truetype(font_path,21)
    small=ImageFont.truetype(font_path,17)
    def panel(name,title,text):
        img=Image.new('RGBA',(1600,100),(15,23,42,255))
        draw=ImageDraw.Draw(img)
        draw.rectangle((0,0,1600,2),fill='#38bdf8')
        draw.text((30,12),title,font=small,fill='#7dd3fc')
        draw.text((30,48),text,font=font,fill='#f8fafc')
        path=work/name;img.save(path);return path
    intro=panel('intro.png','AnimFlow｜可观测、可恢复的 AI 任务管线',
                '真实浏览器连续实录；中文合成旁白。所有任务状态来自实际 API。')
    ending=panel('ending.png','演示结束｜源码与测试证据',
                 'github.com/tangfire/AnimFlow ｜ 75 项自动化测试；真实画面，中文合成旁白。')
    badge=Image.new('RGBA',(180,34),(15,23,42,230))
    ImageDraw.Draw(badge).text((15,5),'中文合成旁白',font=small,fill='#7dd3fc')
    badge_path=work/'synthetic-label.png';badge.save(badge_path)
    (ROOT/'deliverables').mkdir(exist_ok=True)
    final=ROOT/'deliverables'/'AnimFlow-demo-narrated.mp4'
    filters=("[0:v][2:v]overlay=0:800:enable='lt(t,1.2)'[v1];"
             "[v1][3:v]overlay=0:800:enable='gte(t,197.4)'[v2];"
             "[v2][4:v]overlay=1200:4[v]")
    command=[ffmpeg,'-y','-i',str(source),'-i',str(narration),
             '-loop','1','-i',str(intro),'-loop','1','-i',str(ending),
             '-loop','1','-i',str(badge_path),'-filter_complex',filters,
             '-map','[v]','-map','1:a','-t',str(seconds),'-c:v','libx264','-preset','fast',
             '-crf','21','-pix_fmt','yuv420p','-c:a','aac','-b:a','160k',
             '-movflags','+faststart',str(final)]
    with (work/'encode.log').open('w',encoding='utf-8') as log:
        subprocess.run(command,stdout=log,stderr=subprocess.STDOUT,check=True)
    evidence={'voice':spec['voice'],'type':'synthetic Chinese narration',
              'source':'AnimFlow-demo.mp4','output':final.name,'duration_seconds':seconds,
              'segments':report,'bytes':final.stat().st_size}
    (ROOT/'deliverables'/'AnimFlow-narration-evidence.json').write_text(
        json.dumps(evidence,ensure_ascii=False,indent=2),encoding='utf-8')
    print('Exported:',final,flush=True)


if __name__=='__main__':main()
