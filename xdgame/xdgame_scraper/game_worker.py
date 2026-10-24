from __future__ import annotations

import logging
import multiprocessing
import os
import signal
import subprocess
import time
from dataclasses import replace
from multiprocessing.connection import Connection

from .database import DetailCandidate
from .detail_enricher import DetailResolution, DetailResolver, EnrichConfig
from .errors import DailyQuotaExceeded


LOGGER = logging.getLogger(__name__)
WORKER_STOP_SECONDS = 2.0


def resolve_game(candidate: DetailCandidate, config: EnrichConfig) -> DetailResolution:
    # 截止时间由持有数据库的主进程监控，不能依赖可能卡住的浏览器调用自行超时。
    context = multiprocessing.get_context("spawn")
    receiver, sender = context.Pipe(duplex=False)
    worker = context.Process(target=run_game_worker, args=(sender, candidate, config))
    deadline = time.monotonic() + config.game_timeout
    active_stages: list[str] = []
    isolated = False
    try:
        worker.start()
        sender.close()
        while time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            if receiver.poll(min(0.5, max(0.0, remaining))):
                try:
                    message = receiver.recv()
                except EOFError:
                    break
                kind = message[0]
                if kind == "ready":
                    isolated = True
                elif kind in ("stage", "result"):
                    log_stage(candidate.id, message, active_stages)
                elif kind == "resolution":
                    worker.join(max(0.0, deadline - time.monotonic()))
                    if worker.exitcode == 0:
                        return message[1]
                    break
                elif kind == "failure":
                    worker.join(min(WORKER_STOP_SECONDS, max(0.0, deadline - time.monotonic())))
                    if message[1] == "quota":
                        raise DailyQuotaExceeded(message[2])
                    raise RuntimeError(f"单游戏工作进程失败：{message[2]}")
            elif not worker.is_alive():
                break
        if worker.is_alive():
            LOGGER.warning(
                "游戏 %d 处理超过 %.1f 秒；最后阶段：%s；停止工作进程",
                candidate.id, config.game_timeout, current_stage(active_stages),
            )
            return DetailResolution(
                None, "单游戏处理超时", "retry", f"处理超时：{current_stage(active_stages)}"
            )
        raise RuntimeError(
            f"游戏 {candidate.id} 工作进程异常退出；最后阶段：{current_stage(active_stages)}"
        )
    finally:
        if worker.pid is not None and worker.is_alive():
            stop_worker(worker, isolated)
        receiver.close()
        sender.close()


def run_game_worker(
    sender: Connection,
    candidate: DetailCandidate,
    config: EnrichConfig,
) -> None:
    try:
        if os.name == "posix":
            # 独立进程组使超时后可以同时回收 Chromium 子进程。
            os.setsid()
        sender.send(("ready",))
        with DetailResolver(replace(config, stage_events=sender.send)) as resolver:
            try:
                resolution = resolver.resolve(candidate)
            except DailyQuotaExceeded as error:
                # 先告知主进程额度状态，再清理浏览器，避免清理卡住时误记为单条超时。
                sender.send(("failure", "quota", str(error)))
                return
        sender.send(("resolution", resolution))
    except DailyQuotaExceeded as error:
        sender.send(("failure", "quota", str(error)))
    except Exception as error:
        sender.send(("failure", "unexpected", type(error).__name__))
    finally:
        sender.close()


def log_stage(game_id: int, message: tuple, active_stages: list[str]) -> None:
    kind, name, status, elapsed = message
    if kind == "result":
        LOGGER.info("游戏 %d 阶段结果：%s；状态=%s", game_id, name, status)
        return
    if elapsed is None:
        LOGGER.info("游戏 %d 阶段%s：%s", game_id, status, name)
    else:
        LOGGER.info("游戏 %d 阶段%s：%s；耗时 %.1f 秒", game_id, status, name, elapsed)
    if status == "开始":
        active_stages.append(name)
    elif name in active_stages:
        active_stages.remove(name)


def current_stage(active_stages: list[str]) -> str:
    return active_stages[-1] if active_stages else "工作进程清理"


def stop_worker(worker: multiprocessing.Process, isolated: bool) -> None:
    if os.name == "posix" and isolated:
        stop_posix_group(worker)
    elif os.name == "nt":
        stop_windows_tree(worker)
    else:
        worker.terminate()
    worker.join(WORKER_STOP_SECONDS)
    if worker.is_alive():
        worker.kill()
        worker.join(WORKER_STOP_SECONDS)
    if worker.is_alive():
        LOGGER.error("工作进程 %d 未能在清理期限内退出", worker.pid)


def stop_posix_group(worker: multiprocessing.Process) -> None:
    for signal_number in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(worker.pid, signal_number)
        except ProcessLookupError:
            return
        except OSError:
            LOGGER.warning("无法确认工作进程 %d 的浏览器进程组已清理", worker.pid)
            return
        if signal_number == signal.SIGTERM:
            worker.join(WORKER_STOP_SECONDS)


def stop_windows_tree(worker: multiprocessing.Process) -> None:
    try:
        subprocess.run(
            ("taskkill", "/PID", str(worker.pid), "/T", "/F"),
            capture_output=True,
            timeout=WORKER_STOP_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        LOGGER.warning("无法确认工作进程 %d 的浏览器子进程已清理", worker.pid)
        worker.terminate()
