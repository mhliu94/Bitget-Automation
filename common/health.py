"""Atomic local runner observations and graceful stop-file controls."""
from datetime import datetime,timezone
import json
import os
from pathlib import Path
import uuid


def process_token(pid):
    # Linux process start ticks prevent reporting a recycled PID as this runner.
    try:
        fields=Path(f"/proc/{int(pid)}/stat").read_text().rsplit(")",1)[1].split()
        return fields[19]
    except (OSError,ValueError,IndexError):
        return None


class Health:
    def __init__(self,path,broker,group,stop_path):
        self.path=Path(path); self.stop_path=Path(stop_path)
        self.path.parent.mkdir(parents=True,exist_ok=True)
        self.data={"broker":broker,"group":group,"pid":os.getpid(),"process_token":process_token(os.getpid()),
                   "run_id":str(uuid.uuid4()),"started_at":datetime.now(timezone.utc).isoformat(),"status":"starting"}

    def update(self,**fields):
        self.data.update(fields,updated_at=datetime.now(timezone.utc).isoformat())
        temp=self.path.with_name(self.path.name+"."+self.data["run_id"]+".tmp")
        temp.write_text(json.dumps(self.data,indent=2,default=str)+"\n")
        os.replace(temp,self.path)

    def should_stop(self):
        return self.stop_path.exists()


def inspect(path):
    data=json.loads(Path(path).read_text())
    token=process_token(data.get("pid"))
    expected=data.get("process_token")
    data["process_alive_verified"]=bool(token and expected and token==expected)
    data["process_check_supported"]=expected is not None
    return data
