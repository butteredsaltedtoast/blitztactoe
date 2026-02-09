import logging
from threading import Thread
import time
from django.apps import AppConfig

logger = logging.getLogger('game')


class GameConfig(AppConfig):
    name = 'game'
    verbose_name = 'Tic Tac Toe Game'

    def ready(self):
        try:
            cleanup_thread = Thread(target=self._run_cleanup_scheduler, daemon=True)
            cleanup_thread.start()
            logger.info("Game cleanup scheduler started")
        except Exception as e:
            logger.warning(f"Failed to start cleanup scheduler: {e}")
    
    def _run_cleanup_scheduler(self):
        from game.consumers import cleanup_inactive_games
        
        cleanup_interval = 300
        while True:
            try:
                time.sleep(cleanup_interval)
                cleanup_inactive_games()
            except Exception as e:
                logger.error(f"Error in cleanup scheduler: {e}")
