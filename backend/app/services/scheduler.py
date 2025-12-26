import asyncio
import json
import re
import queue
import threading
from datetime import datetime
from typing import TYPE_CHECKING, Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import async_session
from app.models.task import Task
from app.models.execution import Execution, ExecutionStatus
from app.models.notification import NotificationChannel
from app.services.adb import run_adb
from app.services.autoglm import AutoGLMService
from app.services.recorder import RecorderService
from app.services.notifier import NotifierService, NotificationType
from app.services.execution_events import event_bus


def parse_action_to_object(action: Any) -> dict[str, Any] | None:
    """
    解析 action 为对象，支持多种格式：
    - JSON 对象: {"action": "Tap", "x": 100}
    - 函数调用: Tap(x=100, y=200)
    - 简单名称: Finish 或 [finish]
    """
    if not action:
        return None

    # 已经是字典
    if isinstance(action, dict):
        return action

    # 字符串格式
    if isinstance(action, str):
        action_str = action.strip()
        if not action_str:
            return None

        # 格式1: JSON 对象 {"action": "Tap", "x": 100}
        json_match = re.search(r'\{[\s\S]*?"action"[\s\S]*?\}', action_str, re.IGNORECASE)
        if json_match:
            try:
                return json.loads(json_match.group(0))
            except json.JSONDecodeError:
                pass

        # 格式2: 函数调用格式 Tap(x=100, y=200) 或 Launch(package="com.xxx")
        func_match = re.match(r'^(\w+)\((.*)\)$', action_str, re.DOTALL)
        if func_match:
            action_name = func_match.group(1)
            params_str = func_match.group(2)
            result: dict[str, Any] = {"action": action_name}

            # 解析参数
            param_regex = re.compile(r'(\w+)=("([^"]*)"|\'([^\']*)\'|(\d+)|(\w+))')
            for match in param_regex.finditer(params_str):
                key = match.group(1)
                # 优先使用双引号内容，其次单引号，然后数字，最后标识符
                value: Any = match.group(3) or match.group(4)
                if value is None:
                    if match.group(5):
                        value = int(match.group(5))
                    else:
                        value = match.group(6)
                result[key] = value

            return result

        # 格式3: 简单的动作名称 如 Finish 或 [finish]
        simple_match = re.match(r'^\[?(\w+)\]?$', action_str, re.IGNORECASE)
        if simple_match:
            return {"action": simple_match.group(1)}

    return None

if TYPE_CHECKING:
    from apscheduler.job import Job


class SchedulerService:
    """任务调度服务"""

    _instance: "SchedulerService | None" = None

    def __init__(self):
        self.scheduler = AsyncIOScheduler()
        self.autoglm = AutoGLMService()
        self.recorder = RecorderService()
        self.notifier = NotifierService()

    @classmethod
    def get_instance(cls) -> "SchedulerService":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    async def start(self):
        """启动调度器并加载已有任务"""
        if not self.scheduler.running:
            self.scheduler.start()
            await self.load_tasks()

    async def shutdown(self):
        """关闭调度器"""
        if self.scheduler.running:
            self.scheduler.shutdown()

    async def load_tasks(self):
        """从数据库加载所有启用的任务"""
        async with async_session() as session:
            result = await session.execute(
                select(Task).where(Task.enabled == True)
            )
            tasks = result.scalars().all()
            for task in tasks:
                self.add_job(task)

    def add_job(self, task: Task):
        """添加定时任务"""
        job_id = f"task_{task.id}"

        # 移除已存在的任务
        if self.scheduler.get_job(job_id):
            self.scheduler.remove_job(job_id)

        if task.enabled:
            # 解析 cron 表达式，使用本地时区
            cron_parts = task.cron_expression.split()
            local_tz = datetime.now().astimezone().tzinfo
            trigger = CronTrigger(
                minute=cron_parts[0] if len(cron_parts) > 0 else "*",
                hour=cron_parts[1] if len(cron_parts) > 1 else "*",
                day=cron_parts[2] if len(cron_parts) > 2 else "*",
                month=cron_parts[3] if len(cron_parts) > 3 else "*",
                day_of_week=cron_parts[4] if len(cron_parts) > 4 else "*",
                timezone=local_tz,
            )

            self.scheduler.add_job(
                self.execute_task,
                trigger,
                id=job_id,
                args=[task.id],
                replace_existing=True,
            )

    def remove_job(self, task_id: int):
        """移除定时任务"""
        job_id = f"task_{task_id}"
        if self.scheduler.get_job(job_id):
            self.scheduler.remove_job(job_id)

    def get_next_run_time(self, task_id: int) -> datetime | None:
        """获取下次执行时间"""
        job_id = f"task_{task_id}"
        job = self.scheduler.get_job(job_id)
        if job:
            return job.next_run_time
        return None

    async def _get_first_device(self) -> tuple[str | None, str | None]:
        """获取第一个已连接的设备信息"""
        try:
            stdout, _ = await run_adb("devices", "-l")
            output = stdout.decode()

            lines = output.strip().split("\n")[1:]  # 跳过标题行
            for line in lines:
                if not line.strip():
                    continue
                parts = line.split()
                if len(parts) >= 2 and parts[1] == "device":
                    serial = parts[0]
                    model = None
                    for part in parts[2:]:
                        if part.startswith("model:"):
                            model = part.split(":")[1]
                            break
                    return serial, model
        except Exception:
            pass
        return None, None

    async def execute_task(self, task_id: int):
        """执行任务（定时任务调用），使用与 run_task_with_execution 相同的配置加载逻辑"""
        from sqlalchemy import select as sql_select
        from app.models.settings import SystemSettings
        from app.config import get_settings

        settings = get_settings()

        async with async_session() as session:
            # 获取任务信息
            result = await session.execute(select(Task).where(Task.id == task_id))
            task = result.scalar_one_or_none()
            if not task:
                return

            # 获取设备信息
            device_serial, device_model = await self._get_first_device()

            # 创建执行记录
            execution = Execution(
                task_id=task_id,
                status=ExecutionStatus.RUNNING,
                started_at=datetime.utcnow(),
            )
            session.add(execution)
            await session.commit()
            await session.refresh(execution)

            # 从数据库加载设置
            result = await session.execute(sql_select(SystemSettings))
            db_settings = {s.key: s.value for s in result.scalars().all()}

            base_url = db_settings.get("autoglm_base_url") or settings.autoglm_base_url
            api_key = db_settings.get("autoglm_api_key") or settings.autoglm_api_key
            model = db_settings.get("autoglm_model") or settings.autoglm_model
            max_steps = int(db_settings.get("autoglm_max_steps") or settings.autoglm_max_steps)
            lang = settings.autoglm_lang

            # 获取系统提示词规则
            system_prompt, prefix_prompt, suffix_prompt = await self.autoglm.get_system_prompts(
                session, device_serial, device_model
            )
            cmd = self.autoglm.apply_prompt_rules(task.command, prefix_prompt, suffix_prompt)

            try:
                # 开始录屏
                recording_path = await self.recorder.start_recording(execution.id)

                # 直接使用 PhoneAgent
                from phone_agent import PhoneAgent
                from phone_agent.model import ModelConfig
                from phone_agent.agent import AgentConfig
                from phone_agent.config import get_system_prompt

                # 使用队列实现实时步骤更新
                step_queue: queue.Queue = queue.Queue()
                result_holder = {"success": False, "error_msg": None}

                def run_agent_sync():
                    """在线程池中同步运行 agent，通过队列传递步骤"""
                    model_config = ModelConfig(
                        base_url=base_url,
                        api_key=api_key,
                        model_name=model,
                        max_tokens=3000,
                        temperature=0.0,
                        top_p=0.85,
                        frequency_penalty=0.2,
                    )

                    final_system_prompt = get_system_prompt(lang)
                    if system_prompt:
                        final_system_prompt = f"{final_system_prompt}\n\n# 额外规则\n{system_prompt}"

                    agent_config = AgentConfig(
                        max_steps=max_steps,
                        device_id=device_serial,
                        lang=lang,
                        system_prompt=final_system_prompt,
                        verbose=True,
                    )
                    agent = PhoneAgent(model_config=model_config, agent_config=agent_config)

                    try:
                        step_result = agent.step(cmd)

                        while True:
                            # 确保 thinking 是字符串
                            thinking_str = str(step_result.thinking) if step_result.thinking else ""

                            # 使用通用解析函数解析 action
                            action_obj = parse_action_to_object(step_result.action)
                            action_str = str(step_result.action) if step_result.action else ""

                            step_info = {
                                "step": agent.step_count,
                                "thinking": thinking_str,
                                "action": action_obj or action_str,
                                "description": f"<thinking>{thinking_str}</thinking>\n<answer>{action_str}</answer>" if thinking_str else action_str,
                                "timestamp": datetime.utcnow().isoformat(),
                            }
                            # 将步骤放入队列
                            step_queue.put(("step", step_info))

                            if step_result.finished:
                                result_holder["success"] = step_result.success
                                break

                            if agent.step_count >= max_steps:
                                result_holder["error_msg"] = "已达到最大步数限制"
                                break

                            step_result = agent.step()

                    except Exception as e:
                        result_holder["error_msg"] = str(e)
                    finally:
                        agent.reset()
                        # 发送完成信号
                        step_queue.put(("done", None))

                # 启动 agent 线程
                agent_thread = threading.Thread(target=run_agent_sync)
                agent_thread.start()

                # 在主协程中处理队列，实时更新数据库
                # 使用非阻塞方式检查队列，确保不阻塞事件循环
                collected_steps = []
                loop = asyncio.get_event_loop()
                while True:
                    # 使用 run_in_executor 将阻塞的 queue.get 放到线程池
                    # 这样不会阻塞事件循环
                    try:
                        msg = await asyncio.wait_for(
                            loop.run_in_executor(None, lambda: step_queue.get(timeout=0.5)),
                            timeout=1.0
                        )
                        msg_type, data = msg
                        if msg_type == "done":
                            # 发布完成事件
                            await event_bus.publish(execution.id, "done", {
                                "success": result_holder["success"],
                                "message": result_holder.get("error_msg"),
                            })
                            break
                        elif msg_type == "step":
                            collected_steps.append(data)
                            # 实时更新数据库
                            execution.steps = collected_steps.copy()
                            await session.commit()
                            # 发布步骤事件
                            await event_bus.publish(execution.id, "step", data)
                    except (queue.Empty, asyncio.TimeoutError):
                        # 队列空或超时，让出控制权给其他协程
                        await asyncio.sleep(0)
                        continue

                # 等待线程完成
                agent_thread.join(timeout=5)
                success = result_holder["success"]
                error_msg = result_holder["error_msg"]

                # 停止录屏并获取实际的录制文件路径
                actual_recording_path = await self.recorder.stop_recording()

                # 更新执行记录
                execution.status = ExecutionStatus.SUCCESS if success else ExecutionStatus.FAILED
                execution.finished_at = datetime.utcnow()
                execution.steps = collected_steps
                # 使用实际的录制路径（stop_recording 会验证文件是否存在）
                execution.recording_path = actual_recording_path
                if error_msg:
                    execution.error_message = error_msg

                await session.commit()

                # 发送通知
                await self._send_notifications(session, task, execution)

            except Exception as e:
                # 停止录屏
                await self.recorder.stop_recording()

                # 更新执行记录为失败
                execution.status = ExecutionStatus.FAILED
                execution.finished_at = datetime.utcnow()
                execution.error_message = str(e)
                await session.commit()

                # 发送失败通知
                await self._send_notifications(session, task, execution)

    async def _send_notifications(
        self, session: AsyncSession, task: Task, execution: Execution
    ):
        """发送通知"""
        should_notify = (
            (execution.status == ExecutionStatus.SUCCESS and task.notify_on_success)
            or (execution.status == ExecutionStatus.FAILED and task.notify_on_failure)
        )

        if not should_notify:
            return

        # 获取通知渠道
        # 如果任务指定了通知渠道，只使用指定的渠道
        # 否则使用所有启用的渠道
        if task.notification_channel_ids:
            result = await session.execute(
                select(NotificationChannel).where(
                    NotificationChannel.enabled == True,
                    NotificationChannel.id.in_(task.notification_channel_ids),
                )
            )
        else:
            result = await session.execute(
                select(NotificationChannel).where(NotificationChannel.enabled == True)
            )
        channels = result.scalars().all()

        status_text = "成功" if execution.status == ExecutionStatus.SUCCESS else "失败"
        title = f"任务执行{status_text}: {task.name}"
        content = f"- 任务: {task.name}\n- 状态: {status_text}\n- 时间: {execution.finished_at}"

        # 添加最后一步的结果消息
        if execution.steps and len(execution.steps) > 0:
            last_step = execution.steps[-1]
            # 从 action 中提取 message（finish action 的结果消息）
            last_action = last_step.get("action")
            if isinstance(last_action, dict):
                result_msg = last_action.get("message")
                if result_msg:
                    content += f"\n- 结果: {result_msg}"
            elif isinstance(last_action, str) and "message=" in last_action:
                # 解析字符串格式的 message
                import re
                msg_match = re.search(r'message="([^"]*)"', last_action)
                if msg_match:
                    content += f"\n- 结果: {msg_match.group(1)}"

        if execution.error_message:
            content += f"\n- 错误: {execution.error_message}"

        for channel in channels:
            await self.notifier.send_notification(
                NotificationType(channel.type),
                channel.config,
                title,
                content,
            )

    async def run_task_now(self, task_id: int):
        """立即执行任务"""
        await self.execute_task(task_id)

    async def run_task_with_execution(self, task_id: int, execution_id: int):
        """执行任务（使用已创建的执行记录），实时保存步骤到数据库，支持流式输出"""
        from sqlalchemy import select as sql_select
        from app.models.settings import SystemSettings
        from app.config import get_settings
        import threading
        import queue

        settings = get_settings()
        steps_list = []  # 用于收集步骤
        step_queue: queue.Queue = queue.Queue()  # 线程安全的步骤队列
        token_queue: queue.Queue = queue.Queue()  # 流式 token 队列
        stop_event = threading.Event()  # 停止信号

        async def save_step(step_info: dict):
            """实时保存步骤到数据库"""
            steps_list.append(step_info)
            async with async_session() as step_session:
                result = await step_session.execute(
                    sql_select(Execution).where(Execution.id == execution_id)
                )
                exec_record = result.scalar_one_or_none()
                if exec_record:
                    exec_record.steps = steps_list.copy()
                    await step_session.commit()

        async def event_publisher_task():
            """后台任务：从队列读取事件并发布SSE（非阻塞）"""
            loop = asyncio.get_event_loop()

            while not stop_event.is_set() or not step_queue.empty() or not token_queue.empty():
                # 优先处理 token 流（打字机效果）
                try:
                    token_msg = await asyncio.wait_for(
                        loop.run_in_executor(None, lambda: token_queue.get(timeout=0.05)),
                        timeout=0.1
                    )
                    msg_type, data = token_msg
                    if msg_type == "token":
                        phase, content, step_num = data
                        await event_bus.publish(execution_id, "token", {
                            "step": step_num,
                            "phase": phase,
                            "content": content,
                        })
                    continue
                except (queue.Empty, asyncio.TimeoutError):
                    pass

                # 处理完整步骤
                try:
                    step_info = await asyncio.wait_for(
                        loop.run_in_executor(None, lambda: step_queue.get(timeout=0.1)),
                        timeout=0.15
                    )
                    await save_step(step_info)
                    # 发布步骤完成事件
                    await event_bus.publish(execution_id, "step", step_info)
                except (queue.Empty, asyncio.TimeoutError):
                    await asyncio.sleep(0)
                    continue

        async with async_session() as session:
            # 获取任务信息
            result = await session.execute(select(Task).where(Task.id == task_id))
            task = result.scalar_one_or_none()
            if not task:
                return

            # 获取执行记录
            result = await session.execute(
                select(Execution).where(Execution.id == execution_id)
            )
            execution = result.scalar_one_or_none()
            if not execution:
                return

            # 获取设备信息
            device_serial, device_model = await self._get_first_device()

            # 从数据库加载设置（参考 debug.py 的方式）
            result = await session.execute(sql_select(SystemSettings))
            db_settings = {s.key: s.value for s in result.scalars().all()}

            base_url = db_settings.get("autoglm_base_url") or settings.autoglm_base_url
            api_key = db_settings.get("autoglm_api_key") or settings.autoglm_api_key
            model = db_settings.get("autoglm_model") or settings.autoglm_model
            max_steps = int(db_settings.get("autoglm_max_steps") or settings.autoglm_max_steps)
            lang = settings.autoglm_lang

            # 获取系统提示词规则
            system_prompt, prefix_prompt, suffix_prompt = await self.autoglm.get_system_prompts(
                session, device_serial, device_model
            )
            cmd = self.autoglm.apply_prompt_rules(task.command, prefix_prompt, suffix_prompt)

            try:
                # 开始录屏
                recording_path = await self.recorder.start_recording(execution.id)

                # 直接使用 PhoneAgent，实现实时步骤保存
                from phone_agent import PhoneAgent
                from phone_agent.model import ModelConfig
                from phone_agent.agent import AgentConfig
                from phone_agent.config import get_system_prompt
                from app.services.streaming_model import patch_phone_agent, unpatch_phone_agent

                # 当前步骤计数（用于 token 回调）
                current_step_holder = {"step": 0}

                def token_callback(phase: str, content: str):
                    """流式 token 回调，将 token 放入队列"""
                    token_queue.put(("token", (phase, content, current_step_holder["step"])))

                def run_agent_sync():
                    """在线程池中同步运行 agent，每步通过队列实时通知"""
                    # 应用流式补丁
                    original_client = patch_phone_agent(token_callback)

                    try:
                        model_config = ModelConfig(
                            base_url=base_url,
                            api_key=api_key,
                            model_name=model,
                            max_tokens=3000,
                            temperature=0.0,
                            top_p=0.85,
                            frequency_penalty=0.2,
                        )

                        final_system_prompt = get_system_prompt(lang)
                        if system_prompt:
                            final_system_prompt = f"{final_system_prompt}\n\n# 额外规则\n{system_prompt}"

                        agent_config = AgentConfig(
                            max_steps=max_steps,
                            device_id=device_serial,
                            lang=lang,
                            system_prompt=final_system_prompt,
                            verbose=True,
                        )
                        agent = PhoneAgent(model_config=model_config, agent_config=agent_config)

                        success = False
                        error_msg = None

                        try:
                            # 第一步 - 更新步骤计数
                            current_step_holder["step"] = 1
                            step_result = agent.step(cmd)

                            while True:
                                # 确保 thinking 是字符串
                                thinking_str = str(step_result.thinking) if step_result.thinking else ""

                                # 使用通用解析函数解析 action
                                action_obj = parse_action_to_object(step_result.action)
                                action_str = str(step_result.action) if step_result.action else ""

                                # 记录步骤并放入队列（实时保存）
                                step_info = {
                                    "step": agent.step_count,
                                    "thinking": thinking_str,
                                    "action": action_obj or action_str,
                                    "description": f"<thinking>{thinking_str}</thinking>\n<answer>{action_str}</answer>" if thinking_str else action_str,
                                    "timestamp": datetime.utcnow().isoformat(),
                                }
                                step_queue.put(step_info)

                                if step_result.finished:
                                    success = step_result.success
                                    break

                                if agent.step_count >= max_steps:
                                    error_msg = "已达到最大步数限制"
                                    break

                                # 继续下一步 - 更新步骤计数
                                current_step_holder["step"] = agent.step_count + 1
                                step_result = agent.step()

                        except Exception as e:
                            error_msg = str(e)
                        finally:
                            agent.reset()
                            stop_event.set()  # 通知保存任务停止

                        return success, error_msg
                    finally:
                        # 恢复原始 ModelClient
                        unpatch_phone_agent(original_client)

                # 启动事件发布后台任务
                publisher_task = asyncio.create_task(event_publisher_task())

                # 使用线程池执行 agent
                loop = asyncio.get_event_loop()
                success, error_msg = await loop.run_in_executor(None, run_agent_sync)

                # 等待所有事件发布完成
                await publisher_task

                # 停止录屏并获取实际的录制文件路径
                actual_recording_path = await self.recorder.stop_recording()

                # 更新执行记录
                execution.status = ExecutionStatus.SUCCESS if success else ExecutionStatus.FAILED
                execution.finished_at = datetime.utcnow()
                execution.steps = steps_list
                # 使用实际的录制路径（stop_recording 会验证文件是否存在）
                execution.recording_path = actual_recording_path
                if error_msg:
                    execution.error_message = error_msg

                await session.commit()

                # 数据库提交后再发布完成事件到 SSE，确保前端能获取到最新状态
                await event_bus.publish(execution_id, "done", {
                    "success": success,
                    "message": error_msg,
                })

                # 发送通知
                await self._send_notifications(session, task, execution)

            except Exception as e:
                # 确保停止保存任务
                stop_event.set()

                # 停止录屏
                await self.recorder.stop_recording()

                # 更新执行记录为失败
                execution.status = ExecutionStatus.FAILED
                execution.finished_at = datetime.utcnow()
                execution.error_message = str(e)
                execution.steps = steps_list  # 保存已收集的步骤
                await session.commit()

                # 数据库提交后再发布失败事件到 SSE，确保前端能获取到最新状态
                await event_bus.publish(execution_id, "done", {
                    "success": False,
                    "message": str(e),
                })

                # 发送失败通知
                await self._send_notifications(session, task, execution)
