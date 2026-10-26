"""Create an original six-second silent text-card video; requires Pillow and FFmpeg."""
from pathlib import Path
import argparse,shutil,subprocess,tempfile
from PIL import Image,ImageDraw,ImageFont

def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--ffmpeg',default='ffmpeg');p.add_argument('--output',type=Path,default=Path('examples/demo.mp4'));a=p.parse_args()
    with tempfile.TemporaryDirectory(prefix='original-video-demo-') as folder:
        for i,title in enumerate(['LOCAL VIDEO','OPEN SOURCE','DEMO COMPLETE']):
            image=Image.new('RGB',(960,540),'#112338');draw=ImageDraw.Draw(image)
            draw.rectangle((60,50,900,490),outline='#66d9d0',width=5)
            draw.text((100,125),title,font=ImageFont.load_default(size=55),fill='white')
            draw.text((100,260),f'CARD {i+1:02d} / 03',font=ImageFont.load_default(size=42),fill='#66d9d0')
            draw.text((100,365),'ORIGINAL OCR DEMO',font=ImageFont.load_default(size=32),fill='white')
            image.save(Path(folder)/f'card_{i:02d}.png')
        a.output.parent.mkdir(parents=True,exist_ok=True)
        subprocess.run([a.ffmpeg,'-hide_banner','-loglevel','error','-y','-framerate','1/2','-i',str(Path(folder)/'card_%02d.png'),'-t','6','-r','30','-c:v','libx264','-pix_fmt','yuv420p','-movflags','+faststart',str(a.output)],check=True)

if __name__=='__main__':main()
