import os
import sys
import logging

class ColoredFormatter(logging.Formatter):
    """Custom logging formatter that adds ANSI colors based on severity levels."""
    
    GREY = "\x1b[38;20m"
    BLUE = "\x1b[34;20m"
    GREEN = "\x1b[32;20m"
    YELLOW = "\x1b[33;20m"
    RED = "\x1b[31;20m"
    BOLD_RED = "\x1b[31;1m"
    CYAN = "\x1b[36;20m"
    RESET = "\x1b[0m"
    
    # Non-colored base formats
    PLAIN_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
    
    # Colored formats
    COLORED_FORMATS = {
        logging.DEBUG: GREY + "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s" + RESET,
        logging.INFO: CYAN + "%(asctime)s" + RESET + " | " + GREEN + "%(levelname)-8s" + RESET + " | " + BLUE + "%(name)s" + RESET + " | %(message)s",
        logging.WARNING: CYAN + "%(asctime)s" + RESET + " | " + YELLOW + "%(levelname)-8s" + RESET + " | " + BLUE + "%(name)s" + RESET + " | " + YELLOW + "%(message)s" + RESET,
        logging.ERROR: CYAN + "%(asctime)s" + RESET + " | " + RED + "%(levelname)-8s" + RESET + " | " + BLUE + "%(name)s" + RESET + " | " + BOLD_RED + "%(message)s" + RESET,
        logging.CRITICAL: CYAN + "%(asctime)s" + RESET + " | " + BOLD_RED + "%(levelname)-8s" + RESET + " | " + BLUE + "%(name)s" + RESET + " | " + BOLD_RED + "%(message)s" + RESET,
    }
    
    def __init__(self, datefmt=None, use_colors=True):
        super().__init__(datefmt=datefmt)
        self.use_colors = use_colors
        
    def format(self, record):
        if not self.use_colors:
            formatter = logging.Formatter(self.PLAIN_FORMAT, datefmt=self.datefmt)
            return formatter.format(record)
            
        log_fmt = self.COLORED_FORMATS.get(record.levelno, self.COLORED_FORMATS[logging.INFO])
        
        # Save original msg to restore it afterwards
        orig_msg = record.msg
        
        # Apply name-based coloring
        if record.name == "crypto_trader.llm":
            MAGENTA = "\x1b[35;20m"
            record.msg = f"{MAGENTA}{record.msg}{self.RESET}"
        elif record.name == "crypto_trader.websocket":
            ORANGE = "\x1b[38;5;208m"
            record.msg = f"{ORANGE}{record.msg}{self.RESET}"
        elif record.name == "crypto_trader.data_feed":
            LIGHT_BLUE = "\x1b[38;5;39m"
            record.msg = f"{LIGHT_BLUE}{record.msg}{self.RESET}"
            
        formatter = logging.Formatter(log_fmt, datefmt=self.datefmt)
        result = formatter.format(record)
        
        # Restore original message to prevent mutating record for other handlers
        record.msg = orig_msg
        return result


def configure_colored_logging(level=logging.INFO):
    """Configure the root logger to use our ColoredFormatter."""
    no_color_env = os.getenv("NO_COLOR", "false").lower() in ("true", "1", "yes")
    force_color_env = os.getenv("FORCE_COLOR", "false").lower() in ("true", "1", "yes")
    
    # IDE integrated terminals (like Cursor/VSCode) often fail the isatty() check 
    # but support ANSI colors perfectly. We default to True unless explicitly disabled.
    if force_color_env:
        use_colors = True
    elif no_color_env:
        use_colors = False
    else:
        use_colors = True
    
    root_logger = logging.getLogger()
    root_logger.setLevel(level)
    
    # Remove any existing handlers to prevent duplicate prints
    for handler in root_logger.handlers[:]:
        root_logger.removeHandler(handler)
        
    # Create console handler with our custom formatter
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(ColoredFormatter(datefmt="%Y-%m-%d %H:%M:%S", use_colors=use_colors))
    root_logger.addHandler(console_handler)
