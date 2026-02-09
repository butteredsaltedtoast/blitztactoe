import time
from collections import defaultdict
import logging

logger = logging.getLogger('game')


class RateLimitMiddleware:
    
    def __init__(self):
        self.message_timestamps = defaultdict(list)
        self.max_messages = 10
        self.window_seconds = 1.0
        
    async def check_rate_limit(self, channel_name):
        now = time.time()
        
        cutoff = now - self.window_seconds
        self.message_timestamps[channel_name] = [
            ts for ts in self.message_timestamps[channel_name] 
            if ts > cutoff
        ]
        
        if len(self.message_timestamps[channel_name]) >= self.max_messages:
            logger.warning(f"Rate limit exceeded for channel {channel_name}")
            return True
            
        self.message_timestamps[channel_name].append(now)
        return False
    
    def cleanup_channel(self, channel_name):
        if channel_name in self.message_timestamps:
            del self.message_timestamps[channel_name]
            logger.debug(f"Cleaned up rate limit tracking for {channel_name}")


rate_limiter = RateLimitMiddleware()
