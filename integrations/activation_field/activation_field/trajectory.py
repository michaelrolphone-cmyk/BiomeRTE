from concurrent.futures import ThreadPoolExecutor
from collections import deque
from .spectra import describe


class Trajectory:
    def __init__(self,window=128,stride=32):
        self.window=window;self.stride=stride;self.tokens=0
        self.buffer=deque(maxlen=window);self.descriptors=[];self.jobs=[]
        self.worker=ThreadPoolExecutor(max_workers=1,thread_name_prefix="field-spectra")

    def add(self,value):
        self.buffer.append(value.tolist());self.tokens+=1
        if len(self.buffer)==self.window and (self.tokens-self.window)%self.stride==0:
            self.jobs.append((self.tokens,self.worker.submit(describe,list(self.buffer))))
        self.collect()

    def collect(self,wait=False):
        pending=[]
        for token,job in self.jobs:
            if wait or job.done():
                try:self.descriptors.append(dict(token_end=token,**job.result()))
                except Exception as exc:self.descriptors.append(dict(token_end=token,error=type(exc).__name__))
            else:pending.append((token,job))
        self.jobs=pending

    def state(self):
        self.collect(wait=True)
        return dict(tokens=self.tokens,buffer=list(self.buffer),descriptors=self.descriptors,window=self.window,stride=self.stride)

    def restore(self,state):
        self.tokens=state['tokens'];self.buffer=deque(state['buffer'],maxlen=self.window);self.descriptors=state['descriptors']

    def close(self):self.worker.shutdown(wait=True)

    def diagnostics(self):
        self.collect()
        return dict(windows_completed=len(self.descriptors),backlog=len(self.jobs),errors=sum('error' in x for x in self.descriptors),covered_tokens=self.tokens,label="trajectory novelty; unvalidated",bucket_assignment="projection-output-row modulo8; attention8+MLP8/layer; v1")
