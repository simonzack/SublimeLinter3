#
# persist.py
# Part of SublimeLinter3, a code checking framework for Sublime Text 3
#
# Written by Ryan Hileman and Aparajita Fishman
#
# Project: https://github.com/SublimeLinter/SublimeLinter3
# License: MIT
#

"""This module provides persistent global storage for the other modules."""

from collections import defaultdict
from copy import deepcopy
import json
import os
from queue import Queue, Empty
import re
import threading
import traceback
import time
import sublime
import sys

from . import util

PLUGIN_NAME = 'SublimeLinter'

# Get the name of the plugin directory, which is the parent of this file's directory
PLUGIN_DIRECTORY = os.path.basename(os.path.dirname(os.path.dirname(__file__)))

LINT_MODES = (
    ('background', 'Lint whenever the text is modified'),
    ('load/save', 'Lint only when a file is loaded or saved'),
    ('save only', 'Lint only when a file is saved'),
    ('manual', 'Lint only when requested')
)

SYNTAX_RE = re.compile(r'/([^/]+)\.tmLanguage$')


class Settings:

    """This class provides global access to and management of plugin settings."""

    def __init__(self):
        self.settings = {}
        self.previous_settings = {}
        self.plugin_settings = None
        self.on_update_callback = None

    def load(self, force=False):
        """Load the plugin settings."""
        if force or not self.settings:
            self.observe()
            self.on_update()
            self.observe_prefs()

    def get(self, setting, default=None):
        """Return a plugin setting, defaulting to default if not found."""
        return self.settings.get(setting, default)

    def set(self, setting, value):
        """
        Set a plugin setting to the given value.

        Clients of this module should always call this method to set a value
        instead of doing settings['foo'] = 'bar'.

        """
        self.copy()
        self.settings[setting] = value

    def copy(self):
        """Save a copy of the plugin settings."""
        self.previous_settings = deepcopy(self.settings)

    def observe_prefs(self, observer=None):
        """Observe changes to the ST prefs."""
        prefs = sublime.load_settings('Preferences.sublime-settings')
        prefs.clear_on_change('sublimelinter-pref-settings')
        prefs.add_on_change('sublimelinter-pref-settings', observer or self.on_prefs_update)

    def observe(self, observer=None):
        """Observer changes to the plugin settings."""
        self.plugin_settings = sublime.load_settings('SublimeLinter.sublime-settings')
        self.plugin_settings.clear_on_change('sublimelinter-persist-settings')
        self.plugin_settings.add_on_change('sublimelinter-persist-settings',
                                           observer or self.on_update)

    def on_update_call(self, callback):
        """Set a callback to call when user settings are updated."""
        self.on_update_callback = callback

    def on_update(self):
        """
        Update state when the user settings change.

        The settings before the change are compared with the new settings.
        Depending on what changes, views will either be redrawn or relinted.

        """

        settings = util.merge_user_settings(self.plugin_settings)
        self.settings.clear()
        self.settings.update(settings)
        need_relint = self.previous_settings.get('@disable', False) != self.settings.get('@disable', False)

        # Clear the path-related caches if the paths list has changed
        if self.previous_settings.get('paths') != self.settings.get('paths'):
            need_relint = True
            util.clear_caches()

        # Add python paths if they changed
        if self.previous_settings.get('python_paths') != self.settings.get('python_paths'):
            need_relint = True
            python_paths = self.settings.get('python_paths', {}).get(sublime.platform(), [])

            for path in python_paths:
                if path not in sys.path:
                    sys.path.append(path)

        # If the syntax map changed, reassign linters to all views
        from .linter import Linter

        if self.previous_settings.get('syntax_map') != self.settings.get('syntax_map'):
            need_relint = True
            Linter.clear_all()
            util.apply_to_all_views(lambda view: Linter.assign(view, reassign=True))

        # If any of the linter settings changed, relint
        if (not need_relint and self.previous_settings.get('linters') != self.settings.get('linters')):
            need_relint = True

        # Update the gutter marks if the theme changed
        if self.previous_settings.get('gutter_theme') != self.settings.get('gutter_theme'):
            self.update_gutter_marks()

        if need_relint:
            Linter.reload()

        if self.on_update_callback:
            self.on_update_callback(need_relint)

    def save(self, view=None):
        """
        Regenerate and save the user settings.

        User settings are updated with the default settings and the defaults
        from every linter, and if the user settings are currently being edited,
        the view is updated.

        """

        self.load()

        # Fill in default linter settings
        settings = self.settings
        linters = settings.pop('linters', {})

        for name, linter in languages.items():
            default = linter.settings().copy()
            default.update(linters.pop(name, {}))

            if '@disable' not in default:
                default['@disable'] = False

            linters[name] = default

        settings['linters'] = linters

        filename = '{}.sublime-settings'.format(PLUGIN_NAME)
        user_prefs_path = os.path.join(sublime.packages_path(), 'User', filename)
        settings_views = []

        if view is None:
            # See if any open views are the user prefs
            for window in sublime.windows():
                for view in window.views():
                    if view.file_name() == user_prefs_path:
                        settings_views.append(view)
        else:
            settings_views = [view]

        if settings_views:
            def replace(edit):
                if not view.is_dirty():
                    j = json.dumps({'user': settings}, indent=4, sort_keys=True)
                    j = j.replace(' \n', '\n')
                    view.replace(edit, sublime.Region(0, view.size()), j)

            for view in settings_views:
                edits[view.id()].append(replace)
                view.run_command('sublimelinter_edit')
                view.run_command('save')
        else:
            user_settings = sublime.load_settings('SublimeLinter.sublime-settings')
            user_settings.set('user', settings)
            sublime.save_settings('SublimeLinter.sublime-settings')

    def on_prefs_update(self):
        """Perform maintenance when the ST prefs are updated."""
        util.generate_color_scheme()

    def update_gutter_marks(self):
        """Update the gutter mark info based on the the current "gutter_theme" setting."""

        theme = self.settings.get('gutter_theme', 'Default')

        if theme.lower() == 'none':
            gutter_marks['warning'] = gutter_marks['error'] = ''
            return

        theme_path = None

        # User themes override built in themes, check them first
        paths = (
            ('User', 'SublimeLinter-gutter-themes', theme),
            (PLUGIN_DIRECTORY, 'gutter-themes', theme),
            (PLUGIN_DIRECTORY, 'gutter-themes', 'Default')
        )

        for path in paths:
            sub_path = os.path.join(*path)
            full_path = os.path.join(sublime.packages_path(), sub_path)

            if os.path.isdir(full_path):
                theme_path = sub_path
                break

        if theme_path:
            if theme != 'Default' and os.path.basename(theme_path) == 'Default':
                printf('cannot find the gutter theme \'{}\', using the default'.format(theme))

            for error_type in ('warning', 'error'):
                path = os.path.join(theme_path, '{}.png'.format(error_type))
                gutter_marks[error_type] = util.package_relative_path(path)

            path = os.path.join(sublime.packages_path(), theme_path, 'colorize')
            gutter_marks['colorize'] = os.path.exists(path)
        else:
            sublime.error_message(
                'SublimeLinter: cannot find the gutter theme "{}",'
                ' and the default is also not available. '
                'No gutter marks will display.'.format(theme)
            )
            gutter_marks['warning'] = gutter_marks['error'] = ''


class Daemon:

    """
    This class provides a threaded queue that dispatches lints.

    The following operations can be added to the queue:

    hit - Queue a lint for a given view
    delay - Queue a delay for a number of milliseconds
    reload - Indicates the main plugin was reloaded

    """

    MIN_DELAY = 0.1
    running = False
    callback = None
    q = Queue()
    last_runs = {}

    def start(self, callback):
        """Start the daemon thread that runs loop."""
        self.callback = callback

        if self.running:
            self.q.put('reload')
        else:
            # Make sure the system python 3 paths are available to plugins.
            # We do this here to ensure it is only done once, even if the
            # sublimelinter module is reloaded.
            sys.path.extend(util.get_python_paths())

            self.running = True
            threading.Thread(target=self.loop).start()

    def loop(self):
        """Continually check the queue for new items and process them."""

        last_runs = {}

        while True:
            try:
                try:
                    item = self.q.get(block=True, timeout=self.MIN_DELAY)
                except Empty:
                    for view_id, (timestamp, delay) in last_runs.copy().items():
                        # Lint the view if we have gone past the time
                        # at which the lint wants to run.
                        if time.monotonic() > timestamp + delay:
                            self.last_runs[view_id] = time.monotonic()
                            del last_runs[view_id]
                            self.lint(view_id, timestamp)

                    continue

                if isinstance(item, tuple):
                    view_id, timestamp, delay = item

                    if view_id in self.last_runs and timestamp < self.last_runs[view_id]:
                        continue

                    last_runs[view_id] = timestamp, delay

                elif isinstance(item, (int, float)):
                    time.sleep(item)

                elif isinstance(item, str):
                    if item == 'reload':
                        printf('daemon detected a reload')
                        self.last_runs.clear()
                        last_runs.clear()
                else:
                    printf('unknown message sent to daemon:', item)
            except:
                printf('error in SublimeLinter daemon:')
                printf('-' * 20)
                printf(traceback.format_exc())
                printf('-' * 20)

    def hit(self, view):
        """Add a lint request to the queue, return the time at which the request was enqueued."""
        timestamp = time.monotonic()
        self.q.put((view.id(), timestamp, self.get_delay(view)))
        return timestamp

    def delay(self, milliseconds=100):
        """Add a millisecond delay to the queue."""
        self.q.put(milliseconds / 1000.0)

    def lint(self, view_id, timestamp):
        """
        Call back into the main plugin to lint the given view.

        timestamp is used to determine if the view has been modified
        since the lint was requested.

        """
        self.callback(view_id, timestamp)

    def get_delay(self, view):
        """
        Return the delay between a lint request and when it will be processed.

        If a "delay" setting is not available in any of the settings, MIN_DELAY is used.

        """

        delay = (util.get_view_rc_settings(view) or {}).get("delay")

        if delay is None:
            delay = settings.get("delay", self.MIN_DELAY)

        return delay

if not 'queue' in globals():
    queue = Daemon()
    settings = Settings()

    # A mapping between view ids and errors, which are line:(col, message) dicts
    errors = {}

    # A mapping between view ids and HighlightSets
    highlights = {}

    # A mapping between language names and linter classes
    languages = {}

    # A mapping between view ids and a set of linter instances
    linters = {}

    # A mapping between view ids and views
    views = {}

    edits = defaultdict(list)

    # Info about the gutter mark icons
    gutter_marks = {'warning': 'Default', 'error': 'Default', 'colorize': True}

    # Set to true when the plugin is loaded at startup
    plugin_is_loaded = False


def get_syntax(view):
    """Return the view's syntax or the syntax it is mapped to in the "syntax_map" setting."""
    view_syntax = view.settings().get('syntax', '')
    mapped_syntax = ''

    if view_syntax:
        match = SYNTAX_RE.search(view_syntax)

        if match:
            view_syntax = match.group(1).lower()
            mapped_syntax = settings.get('syntax_map', {}).get(view_syntax, '').lower()
        else:
            view_syntax = ''

    return mapped_syntax or view_syntax


def edit(vid, edit):
    """Perform an operation on a view with the given edit object."""
    callbacks = edits.pop(vid, [])

    for c in callbacks:
        c(edit)


def view_did_close(vid):
    """Remove all references to the given view id in persistent storage."""
    if vid in errors:
        del errors[vid]

    if vid in highlights:
        del highlights[vid]

    if vid in linters:
        del linters[vid]

    if vid in views:
        del views[vid]


def debug(*args):
    """Print args to the console if the "debug" setting is True."""
    if settings.get('debug'):
        printf(*args)


def printf(*args):
    """Print args to the console, prefixed by the plugin name."""
    print(PLUGIN_NAME + ': ', end='')

    for arg in args:
        print(arg, end=' ')

    print()


def register_linter(linter_class, name, attrs):
    """Add a linter class to our mapping of languages <--> linter classes."""
    if name:
        name = name.lower()
        linter_class.name = name
        languages[name] = linter_class

        linter_settings = settings.get('linters', {})
        linter_class.lint_settings = linter_settings.get(name, {})

        # The sublime plugin API is not available until plugin_loaded is executed
        if plugin_is_loaded:
            settings.load(force=True)

            # If a linter is reloaded, we have to reassign linters to all views
            from . import linter

            for view in views.values():
                linter.Linter.assign(view, reassign=True)

            printf('{} linter reloaded'.format(linter_class.__name__))
        else:
            printf('{} linter loaded'.format(linter_class.__name__))
