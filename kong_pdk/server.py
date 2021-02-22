import sys
import os
import time
import json
import multiprocessing
# patch connection API so it share with gevent.queue.Channel
from multiprocessing import connection
connection.Connection.get = connection.Connection.recv
connection.Connection.put = connection.Connection.send

from gevent import sleep as gsleep, spawn as gspawn
from gevent.lock import Semaphore
from gevent.queue import Channel

from .pdk import Kong
from .module import Module, load_module
from .exception import PluginServerException
from .logger import Logger

exts = ( '.py', '.pyd', '.so' )
entities = ( 'service', 'consumer', 'route', 'plugin', 'credential', 'memory_stats' )

MSG_RET = 'ret'

def locked_by(lock_name):
    def f(fn):
        def wrapper(*args,**kwargs):
            self = args[0]
            lock = getattr(self, lock_name)
            lock.acquire()
            try:
                r = fn(*args, **kwargs)
            except Exception as ex:
                lock.release()
                raise(ex)
            lock.release()
            return r
        return wrapper

    return f

def _handler_event_func(cls_phase, ch):
    cls_phase(Kong(ch).kong)
    ch.put(MSG_RET)

class PluginServer(object):
    def __init__(self, loglevel=Logger.WARNING, expire_ttl=60, plugin_dir=None, multiprocess=True):
        if multiprocess:
            sem = multiprocessing.Semaphore
        else:
            sem = Semaphore

        self.plugin_dir = plugin_dir
        self.plugins = {}
        self.instances = {}
        self.instance_id = 0
        self.i_lock = sem()
        self.events = {}
        self.event_id = 0
        self.e_lock = sem()

        self.logger = Logger()
        self.logger.set_level(loglevel)

        if plugin_dir:
            self._load_plugins()

        self.multiprocess = multiprocess
        if multiprocess:
            self._process_pool = multiprocessing.Pool()
            self.logger.debug("plugin server is in multiprocessing mode")

        # start cleanup timer
        gspawn(self._clear_expired_plugins, expire_ttl)
    
    def _clear_expired_plugins(self, ttl):
        while True:
            gsleep(ttl)
            self.i_lock.acquire()
            keys = list(self.instances.keys())
            for iid in keys:
                instance = self.instances[iid]
                if instance.is_expired(ttl):
                    self.logger.debug("cleanup instance #%d of %s" % (iid, instance.name))
                    del(self.instances[iid])
            self.i_lock.release()

    def cleanup(self):
        if self.multiprocess:
            self._process_pool.terminate()
            self._process_pool.join()

    def _load_plugins(self):
        if not self.plugin_dir:
            raise PluginServerException("plugin server is not initialized, call SetPluginDir first")

        for p in os.listdir(self.plugin_dir):
            n, ext = os.path.splitext(p)
            if ext in exts:
                path = os.path.join(self.plugin_dir, p)
                try:
                    mod = Module(n, path=path)
                except Exception as ex:
                    self.logger.warn("error loading plugin \"%s\": %s" % (n, ex))
                else:
                    self.logger.debug("loaded plugin \"%s\" from %s" % (n, path))
                    self.plugins[n] = mod

    def set_plugin_dir(self, dir):
        if not os.path.exists(dir):
            return None, dir + " not exists"
        self.plugin_dir = dir
        return "ok", None

    @locked_by("i_lock")
    def get_status(self, *_):
        plugin_status = {}
        for name in self.plugins:
            instances = []
            for iid in self.instances:
                i, err = self.instance_status(iid)
                if err:
                    raise PluginServerException(err)
                instances.append(i)
            plugin = self.plugins[name]
            plugin_status[name] = {
                "Name": name,
                "Modtime": plugin.mtime,
                "LoadTime": plugin.load_time,
                "Instances": instances,
                "LastStartInstance": plugin.last_start_instance_time,
                "LastCloseInstance": plugin.last_close_instance_time,
            }
        return {
            "Pid": os.getpid(),
            "Plugins": plugin_status,
        }, None

    def get_plugin_info(self, name):
        if name not in self.plugins:
            raise PluginServerException(" not initizlied" % name)

        plugin = self.plugins[name]

        info = {
            "Name" : name,
            "Phases" : plugin.phases,
            "Priority": plugin.priority,
            "Schema": {
                "name": name,
                "fields": [{
                    "config": {
                        "type": "record",
                        "fields": plugin.schema,
                    }
                }],
            },
        }
        return info, None
        
    @locked_by("i_lock")
    def start_instance(self, cfg):
        name = cfg['Name']
        if name not in self.plugins:
            raise PluginServerException(" not initizlied" % name)
        plugin = self.plugins[name]

        config = json.loads(cfg['Config'])
        iid = self.instance_id
        self.instances[iid] = plugin.new(config)
        self.instance_id = iid + 1
        
        self.logger.info("instance #%d of %s started" % (iid, name))

        return {
            "Name": name,
            "Id": iid,
            "Config": config,
            "StartTime": time.time()
        }, None

    def instance_status(self, iid):
        if iid not in self.instances:
            return None, "instance #%s not found" % iid
        ins = self.instances[iid]
        return {
            "Name": ins.name,
            "Id": iid,
            "Config": ins.config,
            "StartTime": ins.start_time,
        }, None

    @locked_by("i_lock")
    def close_instance(self, iid):
        if iid not in self.instances:
            return None, "instance #%s not found" % iid
        ins = self.instances[iid]
        ins.close_cb()
        del(self.instances[iid])
        return {
            "Name": ins['name'],
            "Id": iid,
            "Config": ins['config'],
        }, None

    @locked_by("e_lock")
    def handle_event(self, event):
        iid = event['InstanceId']
        if iid not in self.instances:
            raise PluginServerException("instance id %s not found" % iid)
        instance = self.instances[iid]
        instance.reset_expire_ts()
        cls = instance.cls
        phase = event['EventName']

        eid = self.event_id
        self.event_id = eid + 1

        if self.multiprocess:
            ch, child_ch = multiprocessing.Pipe(duplex=True)
            self.events[eid] = ch
            self._process_pool.apply_async(
                _handler_event_func,
                (getattr(cls, phase), child_ch),
            )
        else:
            # plugin communites to Kong (RPC client) in a reverse way
            ch = Channel()
            self.events[eid] = ch

            gspawn(_handler_event_func,
                getattr(cls, phase), ch,
            )

        r = ch.get()
        instance.reset_expire_ts()

        return {
            "Data": r,
            "EventId": eid,
        }, None
    
    def _step(self, data, is_error):
        eid = data['EventId']
        if eid not in self.events:
            raise PluginServerException("event id %s not found" % eid)
        dd = None
        if 'Data' in data:
            dd = data['Data']
        ch = self.events[eid]
        if is_error:
            ch.put((
                None, dd
            ))
        else:
            ch.put((
                dd, None
            ))

        ret = ch.get()

        if ret == MSG_RET:
            del self.events[eid]

        return {
            "Data": ret,
            "EventId": eid,
        }, None

    def step(self, data):
        return self._step(data, False)

    def step_error(self, data):
        return self._step(data, True)

for entity in entities:
    setattr(PluginServer, 'step_' + entity, PluginServer.step)

setattr(PluginServer, 'step_multi_map', PluginServer.step)