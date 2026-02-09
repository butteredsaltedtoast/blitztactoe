"""
Management command to clean up inactive games.
Usage: python manage.py cleanup_games
"""
from django.core.management.base import BaseCommand
from game.consumers import cleanup_inactive_games
import logging

logger = logging.getLogger('game')


class Command(BaseCommand):
    help = 'Clean up inactive and abandoned games from memory'

    def handle(self, *args, **options):
        self.stdout.write(self.style.SUCCESS('Starting game cleanup...'))
        try:
            cleanup_inactive_games()
            self.stdout.write(self.style.SUCCESS('Game cleanup completed successfully'))
        except Exception as e:
            logger.error(f"Error during game cleanup: {e}", exc_info=True)
            self.stdout.write(self.style.ERROR(f'Error during cleanup: {e}'))
