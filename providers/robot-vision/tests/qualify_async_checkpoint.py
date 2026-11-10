"""Native checkpoint/track/restore qualification against retained data."""
def main():
    import pathlib,sys,json,time,threading,base64
    root=pathlib.Path('/Users/micahelbyrns/Developer/biomerte-async-slam-20261110')
    sys.path[:0]=[str(root),str(root/'providers/robot-vision')]
    from slam_backend import SlamBackend
    from biome_provider import VisionClient
    import cv2,numpy as np
    home=pathlib.Path.home()/'.local/share/biome-robot-vision'
    packages=[]
    for f in (home/'observations/checkpoints').glob('*/manifest.json'):
     try:
      v=json.loads(f.read_text());a=v.get('backend_artifact',{})
      if a.get('availability')=='available':packages.append((v['created_at'],v))
     except Exception:pass
    v=max(packages)[1];path=pathlib.Path(v['backend_artifact']['path']);m=json.loads((path/'backend_manifest.json').read_text())
    cfg={'calibration':m['calibration'],'mounting':m.get('mounting'),'fps':5,'pyslam_root':str(home/'vendor/pyslam')}
    b=SlamBackend(cfg,home)
    try:
     print('START',v['checkpoint_id'],flush=True);b.start(m['map_id'],v['backend_artifact'])
     response=VisionClient(home/'vision.sock',timeout=10).query('robot.vision.snapshot',{'include_frame':True})
     raw=base64.b64decode(response['frame']['data_base64']);image=cv2.imdecode(np.frombuffer(raw,np.uint8),cv2.IMREAD_COLOR)
     out=pathlib.Path('/tmp/async-slam-native-candidate-20261110');out.mkdir(exist_ok=True)
     target=out/str(time.time_ns());t=time.monotonic();ticket=b.capture_checkpoint(target);capture_s=time.monotonic()-t
     result=[];error=[]
     def write():
      try:result.append(b.write_checkpoint(ticket))
      except Exception as e:error.append(repr(e))
     writer=threading.Thread(target=write);writer.start();times=[]
     for i in range(12):
      t=time.monotonic();b.track(image,'native-qualification:'+str(i),t);times.append(round(time.monotonic()-t,4));time.sleep(.1)
     writer.join();print('RESULT',json.dumps({'capture_s':capture_s,'track_seconds':times,'error':error,'saved':bool(result),'target':str(target)}),flush=True)
     assert not error and result
     b.close();b=SlamBackend(cfg,home);b.start(m['map_id'],{'availability':'available','path':str(target)})
     print('RESTORE',b.status()['status'],flush=True)
    finally:b.close()

if __name__ == "__main__":
    main()
