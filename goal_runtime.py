"""One persistent model thread; notebook contents and lifecycle policy come from YAML."""
import subprocess
import threading
from pathlib import Path


def _seed_files(runtime, directory, files, prompt_file):
    directory.mkdir(parents=True, exist_ok=True)
    paths = {}
    for name, content in files.items():
        path = directory / name
        if not isinstance(content, str) or Path(name).is_absolute() or ".." in Path(name).parts:
            raise runtime.Error("Goal files must have relative paths and text contents.")
        if path.is_symlink() or directory not in path.resolve().parents:
            raise runtime.Error(f"Goal file escapes its workspace: {name}")
        paths[name] = path
    if prompt_file not in paths:
        raise runtime.Error("The goal prompt_file must be listed in files.")
    existed = paths[prompt_file].exists()
    if existed and paths[prompt_file].read_bytes() != files[prompt_file].encode("utf-8"):
        raise runtime.Error("The saved initial prompt differs from this assignment; use a new workspace.")
    for name, path in paths.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with path.open("xb") as stream:
                stream.write(files[name].encode("utf-8"))
        except FileExistsError:
            pass
    return existed


def goal_session(runtime, prompt, *, prompts, files, directory, settings, options,
                 prompt_file, node_name="author", stages=None, initial_instruction=""):
    """Yield a completed goal's final answer; send critic feedback to revise it.

    ``files`` maps run-relative names to already-rendered initial contents.
    Existing notebooks are never rewritten by this transport. All model-facing
    continuation, compaction, repair, and resume instructions come from ``prompts``.
    """
    directory = Path(directory).resolve()
    restoring = _seed_files(runtime, directory, files, prompt_file)
    stages = stages or {"initial": "solve", "resume": "repair", "failure": "failure"}
    stage = stages["initial"]
    values = dict(original_prompt=prompt, directory=str(directory), prompt_file=prompt_file,
                  solution="", bugs="", round=0, revision_number=0, instruction=initial_instruction)

    def render(name):
        return runtime.render_template(prompts[name], values)

    def emit(kind, **fields):
        runtime.emit(kind, stage, node=node_name, **fields)

    settings = dict(settings)
    model = settings["model"]
    settings["effort"] = runtime.effective_effort(model, settings["effort"])
    settings["speed"] = runtime.effective_speed(model, settings["speed"])
    summary = settings["summary"] = runtime.reasoning_summary(model, settings["summary"])
    thread = None
    rpc = process = None
    stop = threading.Event()
    expired = threading.Event()
    state = {"turn": None, "active": False, "steer": None}
    pending_steers = {}
    pending_compaction = None
    seen_compactions = set()
    goal = {}
    failure = None
    last_usage = {}

    def record(message):
        event = runtime.public_event(message)
        if event is not None:
            emit("codex_event", event=event,
                 root=event.get("params", {}).get("threadId") in {None, thread})

    def steer(instruction, identity, compaction=False):
        request = rpc.request("turn/steer", {
            "threadId": thread, "expectedTurnId": state["turn"],
            "input": [{"type": "text", "text": instruction}],
        })
        pending_steers[request] = (identity, instruction, compaction)
        emit("request", label="Author context re-anchor after compaction" if compaction
             else "Live author instruction sent", text=instruction, threadId=thread)

    def watch():
        while not stop.wait(0.1):
            if runtime.workflow_remaining(options) <= 0:
                expired.set()
                try:
                    if thread:
                        rpc.request("thread/goal/set", {**goal, "status": "paused"})
                        if state["turn"]:
                            rpc.request("turn/interrupt", {"threadId": thread, "turnId": state["turn"]})
                except (runtime.Error, OSError):
                    pass
                if not stop.wait(getattr(runtime, "INTERRUPT_GRACE_SECONDS", 5)):
                    runtime.stop_process(process)
                return
            if state["active"] and state["turn"]:
                command = runtime.pending_author_steer(options.get("author_steer_file"), state["steer"])
                if command and not any(item[0] == command[0] for item in list(pending_steers.values())):
                    try:
                        steer(command[1], command[0])
                    except (runtime.Error, OSError) as exc:
                        emit("diagnostic", text=f"Could not send live instruction: {exc}")

    def start_turn(instruction):
        if expired.is_set() or runtime.workflow_remaining(options) <= 0:
            raise runtime.Error("Workflow time limit reached; saved files are unchanged.")
        rpc.call("thread/goal/set", {**goal, "status": "paused"})
        emit("request", label="Exact solve input", text=instruction, threadId=thread,
             model=model, reasoningEffort=settings["effort"], reasoningSummary=summary)
        state["active"] = True
        result = rpc.call("turn/start", {"threadId": thread, "cwd": str(directory),
            "input": [{"type": "text", "text": instruction}], "summary": summary})
        state["turn"] = (result.get("turn") or {}).get("id")
        if expired.is_set() or runtime.workflow_remaining(options) <= 0:
            raise runtime.Error("Workflow time limit reached; saved files are unchanged.")
        rpc.call("thread/goal/set", {**goal, "status": "active"})

    def collect(item, answers):
        if (item.get("type") in {"agentMessage", "agent_message"}
                and item.get("phase") in {None, "final_answer"}
                and isinstance(item.get("text"), str) and item["text"].strip()):
            answers.append(item["text"])

    try:
        if runtime.workflow_remaining(options) <= 0:
            raise runtime.Error("Workflow time limit reached; saved files are unchanged.")
        process = subprocess.Popen([
            runtime.codex(), "app-server", "--enable", "goals", "--disable", "multi_agent",
            *runtime.provider_arguments(model), *runtime.speed_arguments(settings["speed"], model),
            *runtime.context_cache_arguments(),
        ], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, encoding="utf-8", errors="replace", bufsize=1, cwd=str(directory),
            env=runtime.environment(model))
        rpc = runtime.RPC(process, record)
        watcher = threading.Thread(target=watch, daemon=True)
        watcher.start()
        rpc.call("initialize", {"clientInfo": {"name": "tcs_prover", "title": "TCS Prover", "version": "1"}})
        rpc.send({"method": "initialized", "params": {}})
        thread_options = {
            "model": model, "cwd": str(directory), "runtimeWorkspaceRoots": [str(directory)],
            "sandbox": "workspace-write", "approvalPolicy": "never",
            "config": {"model_reasoning_effort": settings["effort"], "model_reasoning_summary": summary,
                "features": {"goals": True, "multi_agent": False, "fast_mode": settings["speed"] == "fast"}},
        }
        if hasattr(runtime, "model_provider"):
            thread_options["modelProvider"] = runtime.model_provider(model)
        resumed = False
        if options.get("goal_thread_id"):
            try:
                result = rpc.call("thread/resume", {**thread_options, "threadId": options["goal_thread_id"],
                                                    "excludeTurns": True})
                resumed = True
            except runtime.Error as exc:
                missing = ("not found", "not_found", "notfound", "unknown thread", "does not exist",
                           "no rollout", "failed to load thread", "failed to load rollout",
                           "could not find thread", "thread unavailable", "thread is unavailable")
                if not any(word in str(exc).lower() for word in missing):
                    raise
                emit("diagnostic", text=f"Saved thread unavailable; restoring from preserved files: {exc}")
        if not resumed:
            result = rpc.call("thread/start", {**thread_options, "ephemeral": False})
        thread = result["thread"]["id"]
        goal = {"threadId": thread, "objective": render("goal")}
        emit("status", label="Goal resumed" if resumed else "Goal started", threadId=thread,
             text=f"Thread {thread}")
        instruction = render("resume") if restoring or options.get("goal_thread_id") else prompt
        if initial_instruction:
            instruction += "\n\n" + initial_instruction
        start_turn(instruction)
        status, running, answers = None, True, []
        while True:
            if expired.is_set() or runtime.workflow_remaining(options) <= 0:
                raise runtime.Error("Workflow time limit reached; saved files are unchanged.")
            message = rpc.read()
            params = message.get("params", {})
            if params.get("threadId") not in {None, thread}:
                continue
            request = message.get("id")
            if request in pending_steers and "method" not in message:
                identity, instruction, compaction = pending_steers.pop(request)
                if "error" in message:
                    if compaction:
                        pending_compaction = instruction
                    emit("diagnostic", text=f"Instruction raced with turn completion: {message['error']}")
                elif not compaction:
                    state["steer"] = identity
            method, turn = message.get("method"), params.get("turn") or {}
            if method == "turn/started":
                running, answers = True, []
                state["turn"] = turn.get("id")
                if pending_compaction and state["turn"]:
                    steer(pending_compaction, "retry", True)
                    pending_compaction = None
            elif method == "item/completed":
                item = params.get("item") or {}
                collect(item, answers)
                if item.get("type") == "contextCompaction":
                    key = (params.get("turnId"), item.get("id"))
                    if key not in seen_compactions:
                        seen_compactions.add(key)
                        state["turn"] = params.get("turnId") or state["turn"]
                        pending_compaction = render("compaction")
                        if state["turn"]:
                            steer(pending_compaction, key, True)
                            pending_compaction = None
            elif method == "thread/goal/updated":
                status = (params.get("goal") or {}).get("status")
            elif method == "thread/tokenUsage/updated":
                last_usage = (params.get("tokenUsage") or {}).get("last") or {}
            elif method == "turn/completed":
                running, state["turn"] = False, None
                if last_usage and hasattr(runtime, "emit_cache_usage"):
                    runtime.emit_cache_usage(stage, last_usage, label="Author cache usage")
                    last_usage = {}
                for item in turn.get("items", []):
                    collect(item, answers)
                if turn.get("status") in {"failed", "interrupted"}:
                    raise runtime.Error(f"Author turn {turn['status']}: {turn.get('error') or 'no complete result'}")
            elif method == "error" and not params.get("willRetry", False):
                raise runtime.Error(str(params.get("error") or params))
            if status in {"usageLimited", "budgetLimited"}:
                raise runtime.Error(f"Author stopped: {status}.")
            if not running and status == "complete" and answers:
                state["active"] = False
                solution = answers[-1]
                emit("status", label="Goal complete", threadId=thread, text=f"Thread {thread}")
                emit("author_result", label="Author solution", text=solution, threadId=thread)
                rejection = yield {"outcome": "proof", "solution": solution}
                if rejection is None:
                    return
                values.update(rejection)
                values["revision_number"] += 1
                stage = stages["resume"]
                start_turn(render("repair"))
                status, running, answers = None, True, []
            elif not running and status in {"blocked", "complete"}:
                start_turn(render("continuation"))
                status, running, answers = None, True, []
    except (runtime.Error, OSError, ValueError) as exc:
        stage = stages["failure"]
        diagnostic = "Workflow time limit reached; saved files remain available." if expired.is_set() else str(exc)
        emit("diagnostic", text=diagnostic, threadId=thread)
        emit("failure_result", label="Author stopped", text=diagnostic, output=diagnostic, threadId=thread)
        failure = {"outcome": "failure", "output": diagnostic}
    finally:
        stop.set()
        if rpc and thread and state["active"]:
            def pause():
                try:
                    rpc.call("thread/goal/set", {**goal, "status": "paused"})
                except (runtime.Error, OSError, ValueError):
                    pass
            cleanup = threading.Thread(target=pause, daemon=True)
            cleanup.start()
            cleanup.join(timeout=1)
        if process:
            runtime.stop_process(process)
    if failure:
        yield failure
