"""Управление Docker-контейнерами через Unix socket"""
import asyncio
import logging
import time

logger = logging.getLogger(__name__)

DOCKER_SOCKET = "/var/run/docker.sock"

# троттлинг: не чаще 1 рестарта в 5 минут
_last_restart: float = 0
_RESTART_COOLDOWN = 300


async def restart_warp() -> bool:
    """Перезапускает контейнер WARP для получения нового IP.
    Возвращает True если рестарт выполнен, False если на кулдауне или ошибка.
    """
    global _last_restart
    now = time.time()
    if now - _last_restart < _RESTART_COOLDOWN:
        logger.info("WARP рестарт на кулдауне (осталось %d сек)", int(_RESTART_COOLDOWN - (now - _last_restart)))
        return False

    try:
        find_cmd = (
            'curl -s --unix-socket /var/run/docker.sock '
            '"http://localhost/containers/json?filters=%7B%22label%22%3A%5B%22com.docker.compose.service%3Dwarp%22%5D%7D"'
        )
        proc = await asyncio.create_subprocess_shell(
            find_cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        stdout, _ = await proc.communicate()

        import json
        containers = json.loads(stdout)
        if not containers:
            logger.warning("WARP контейнер не найден через Docker API")
            return False

        container_id = containers[0]["Id"][:12]
        logger.info("Перезапуск WARP контейнера %s для смены IP...", container_id)

        restart_cmd = (
            f'curl -s --unix-socket /var/run/docker.sock '
            f'-X POST "http://localhost/containers/{container_id}/restart?t=10"'
        )
        proc = await asyncio.create_subprocess_shell(
            restart_cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        await proc.communicate()

        if proc.returncode == 0:
            _last_restart = time.time()
            logger.info("WARP контейнер %s перезапущен", container_id)
            return True
        else:
            logger.warning("Не удалось перезапустить WARP: returncode=%d", proc.returncode)
            return False

    except Exception as e:
        logger.warning("Ошибка при перезапуске WARP: %s", e)
        return False
