# Copyright (c) 2015-present, Facebook, Inc.
# All rights reserved.
#
# This source code is licensed under the license found in the LICENSE file in
# the root directory of this source tree.

from lldb import eStateStepping, eStateRunning, SBBreakpoint, SBEvent, SBListener, SBProcess, SBTarget, SBWatchpoint
from threading import Thread

from handler import HandlerDomain, UndefinedHandlerError, handler
from remote_objects import ValueListRemoteObject
import file_manager
import serialize
from logging_helper import log_debug
from thread_manager import ThreadManager

class ModuleSourcePathUpdater:
    """Register source paths in debug data of modules as they are loaded.

    NB: module in this context are SBModule instances, representing executable
    images including symbol and debug information.
    """
    def __init__(self, target, file_manager, basepath='.'):
        self._registered_modules = set()
        self._target = target
        self._file_manager = file_manager
        self._basepath = basepath

    def modules_updated(self):
        for module in self._target.modules:
            try:
                if module.uuid not in self._registered_modules:
                    self._register_source_paths_for_module(module)
                    self._registered_modules.add(module.uuid)
            except Exception as e:
                # Some module does not have uuid.
                log_debug('Can\'t register module: %s' % str(e))

    def _register_source_paths_for_module(self, module):
        for comp_unit in module.compile_units:
            if comp_unit.file.fullpath is None:
                continue
            self._file_manager.register_filelike(
                file_manager.File.from_filespec(
                    comp_unit.file, self._basepath))


class LLDBListenerThread(Thread):
    should_quit = False

    def __init__(self, server, location_serializer, remote_object_manager,
                 module_source_path_updater, thread_manager, process):
      Thread.__init__(self)
      self.daemon = True

      self.server = server
      self.location_serializer = location_serializer
      self.remote_object_manager = remote_object_manager
      self.module_source_path_updater = module_source_path_updater
      self.listener = SBListener('Chrome Dev Tools Listener')
      self.thread_manager = thread_manager

      self._add_listener_to_process(process)
      self._broadcast_process_state(process)

      self._add_listener_to_target(process.target)

    def _add_listener_to_target(self, target):
        # Listen for breakpoint/watchpoint events (Added/Removed/Disabled/etc).
        broadcaster = target.GetBroadcaster()
        mask = SBTarget.eBroadcastBitBreakpointChanged | SBTarget.eBroadcastBitWatchpointChanged | SBTarget.eBroadcastBitModulesLoaded
        broadcaster.AddListener(self.listener, mask)

    def _add_listener_to_process(self, process):
        # Listen for process events (Start/Stop/Interrupt/etc).
        broadcaster = process.GetBroadcaster()
        mask = SBProcess.eBroadcastBitStateChanged
        broadcaster.AddListener(self.listener, mask)

    def _broadcast_process_state(self, process):
        # Reset the object group so old frame variable objects don't linger
        # forever.
        self.thread_manager.release()
        if process.state == eStateStepping or process.state == eStateRunning:
            self.server.send_notification('Debugger.resumed', None)
        else:
            thread = process.GetSelectedThread()
            self.thread_manager.update(process)
            params = {
              "callFrames": self.thread_manager.get_thread_stack(thread),
              "reason": serialize.StopReason_to_string(thread.GetStopReason()),
              "data": {},
            }
            self.server.send_notification('Debugger.paused', params)

    def _breakpoint_event(self, event):
        breakpoint = SBBreakpoint.GetBreakpointFromEvent(event)
        for location in self.location_serializer.get_breakpoint_locations(breakpoint):
            params = {
                'breakpointId': str(breakpoint.id),
                'location': location,
            }
            self.server.send_notification('Debugger.breakpointResolved', params)

    def _watchpoint_event(self, event):
        # TODO(williamsc) Add support for sending watchpoint change events.
        pass

    def run(self):
        while not self.should_quit:
            event = SBEvent()
            if self.listener.WaitForEvent(1, event):
                if event.GetType() == SBTarget.eBroadcastBitModulesLoaded:
                    self.module_source_path_updater.modules_updated()
                elif SBProcess.EventIsProcessEvent(event):
                    self._broadcast_process_state(SBProcess.GetProcessFromEvent(event))
                elif SBBreakpoint.EventIsBreakpointEvent(event):
                    self._breakpoint_event(event)
                elif SBWatchpoint.EventIsWatchpointEvent(event):
                    self._watchpoint_event(event)


class DebuggerDomain(HandlerDomain):

    def __init__(self, runtimeDomain, fileManager, remoteObjectManager,
                 basepath='.', **kwargs):
        HandlerDomain.__init__(self, **kwargs)
        self.runtimeDomain = runtimeDomain
        self.fileManager = fileManager
        self.remoteObjectManager = remoteObjectManager
        self.locationSerializer = serialize.LocationSerializer(
            fileManager, basepath)
        self.moduleSourcePathUpdater = ModuleSourcePathUpdater(
            self.debugger.GetSelectedTarget(), fileManager, basepath)
        self.thread_manager = ThreadManager(self.socket, self.locationSerializer, self.remoteObjectManager)

    @property
    def name(self):
        return 'Debugger'

    @handler()
    def canSetScriptSource(self, params):
        # Return False, becuase we don't support
        # changing source at runtime.
        return {"result": False}

    @handler()
    def continueToLocation(self, params):
        # TODO(williamsc) - This is probably setting a one off breakpoint and continuing.
        raise UndefinedHandlerError('continueToLocation not implemented')

    @handler()
    def disable(self, params):
        # Not exactly the same as disable. Detach() might be closer to
        # what Chrome Dev Tools is trying to do.
        self.debugger.GetSelectedTarget().DisableAllBreakpoints()
        return {}

    @handler()
    def enable(self, params):
        process = self.debugger.GetSelectedTarget().process
        self.event_thread = LLDBListenerThread(
            server=self.socket,
            location_serializer=self.locationSerializer,
            remote_object_manager=self.remoteObjectManager,
            module_source_path_updater=self.moduleSourcePathUpdater,
            thread_manager = self.thread_manager,
            process=process)
        self.moduleSourcePathUpdater.modules_updated()
        self.event_thread.start()
        return {}

    @handler()
    def evaluateOnCallFrame(self, params):
        frameId = params['callFrameId']

        thread, frame = frameId.split('.')
        # TODO: These return booleans to indicate success. Throw something if False.
        self.debugger.GetSelectedTarget().process.SetSelectedThreadByIndexID(int(thread))
        self.debugger.GetSelectedTarget().process.GetSelectedThread().SetSelectedFrame(int(frame))

        return self.runtimeDomain.evaluate(params)

    @handler()
    def getScriptSource(self, params):
        filelike = self.fileManager.get_by_script_id(params['scriptId'])
        if filelike:
            return {'scriptSource': filelike.script_source}
        else:
            return {'scriptSource': '<Failed to fetch source.>'}

    @handler()
    def pause(self, params):
        self.debugger.GetSelectedTarget().process.Stop()
        return {}

    @handler()
    def removeBreakpoint(self, params):
        self.debugger.GetSelectedTarget().BreakpointDelete(int(params['breakpointId']))
        return {}

    @handler()
    def resume(self, params):
        self.debugger.GetSelectedTarget().process.Continue()
        return {}

    @handler()
    def selectThread(self, params):
        threadId = params['threadId']
        self.debugger.GetSelectedTarget().process.SetSelectedThreadByID(threadId)
        return {}

    @handler()
    def getThreadStack(self, params):
        threadId = params['threadId']
        thread = self.debugger.GetSelectedTarget().process.GetThreadByID(threadId)
        params = { "callFrames": [] }
        if not thread == None:
            params["callFrames"] = self.thread_manager.get_thread_stack(thread)
        return params

    @handler()
    def searchInContent(self, params):
        raise UndefinedHandlerError('searchInContent not implemented')

    @handler()
    def setBreakpoint(self, params):
        filelike = self.fileManager.get_by_script_id(params['location']['scriptId'])
        if not filelike or not isinstance(filelike, file_manager.File):
            # Only support setting breakpoints in real files.
            return {}
        return self._set_breakpoint_by_filespec(
            filelike.server_obj,
            int(params['location']['lineNumber']) + 1)

    @handler()
    def setBreakpointByUrl(self, params):
        filelike = self.fileManager.get_by_client_url(params['url'])
        if not filelike or not isinstance(filelike, file_manager.File):
            raise RuntimeError('Cannot find file for breakpoint.')
        return self._set_breakpoint_by_filespec(
            filelike.server_obj,
            int(params['lineNumber']) + 1)

    @handler()
    def setBreakpointsActive(self, params):
        if params['active']:
            self.debugger.GetSelectedTarget().EnableAllBreakpoints()
        else:
            self.debugger.GetSelectedTarget().DisableAllBreakpoints()
        return {}

    @handler()
    def setPauseOnExceptions(self, params):
        # TODO(williamsc) - Support add support for pausing on exceptions
        raise UndefinedHandlerError('setPauseOnExceptions not implemented')

    @handler()
    def setScriptSource(self, params):
        raise UndefinedHandlerError('setScriptSource not supported for LLDB')

    @handler()
    def stepInto(self, params):
        self.debugger.GetSelectedTarget().GetProcess().GetSelectedThread().StepInto()
        return {}

    @handler()
    def stepOut(self, params):
        self.debugger.GetSelectedTarget().GetProcess().GetSelectedThread().StepOut()
        return {}

    @handler()
    def stepOver(self, params):
        self.debugger.GetSelectedTarget().GetProcess().GetSelectedThread().StepOver()
        return {}

    def _set_breakpoint_by_filespec(self, filespec, line):
        breakpoint = self.debugger.GetSelectedTarget().BreakpointCreateByLocation(filespec, line)
        return {
            'breakpointId': str(breakpoint.id),
            'locations':
                self.locationSerializer.get_breakpoint_locations(breakpoint),
        }
