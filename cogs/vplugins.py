import asyncio
import io
import json
import os
import shutil
import sys
import typing
import zipfile
from importlib import invalidate_caches
from difflib import get_close_matches
from pathlib import Path, PurePath
from re import match
from site import USER_SITE
from subprocess import PIPE

import discord
from discord.ext import commands

from pkg_resources import parse_version

from core import checks
from core.models import PermissionLevel, getLogger
from core.paginator import EmbedPaginatorSession
from core.utils import truncate, trigger_typing

logger = getLogger(__name__)


class InvalidPluginError(commands.BadArgument):
    pass


class vPlugin:
    def __init__(self, user, repo, name, branch=None):
        self.user = user
        self.repo = repo
        self.name = name
        self.branch = branch if branch is not None else "master"
        self.url = f"https://github.com/{user}/{repo}/archive/{self.branch}.zip"
        self.link = f"https://github.com/{user}/{repo}/tree/{self.branch}/{name}"

    @property
    def vpath(self):
        return PurePath("vplugins") / self.user / self.repo / f"{self.name}-{self.branch}"

    @property
    def abs_vpath(self):
        return Path(__file__).absolute().parent.parent / self.vpath

    @property
    def cache_vpath(self):
        return (
            Path(__file__).absolute().parent.parent
            / "vtemp" # bro moment i forgot to rename this
            / "vplugins-cache"
            / f"{self.user}-{self.repo}-{self.branch}.zip"
        )

    @property
    def ext_string(self):
        return f"vplugins.{self.user}.{self.repo}.{self.name}-{self.branch}.{self.name}"

    def __str__(self):
        return f"{self.user}/{self.repo}/{self.name}@{self.branch}"

    def __lt__(self, other):
        return self.name.lower() < other.name.lower()

    @classmethod
    def from_string(cls, s, strict=False):
        if not strict:
            m = match(r"^(.+?)/(.+?)/(.+?)(?:@(.+?))?$", s)
        else:
            m = match(r"^(.+?)/(.+?)/(.+?)@(.+?)$", s)
        if m is not None:
            return vPlugin(*m.groups())
        raise InvalidPluginError("Cannot decipher %s.", s)  # pylint: disable=raising-format-tuple

    def __hash__(self):
        return hash((self.user, self.repo, self.name, self.branch))

    def __repr__(self):
        return f"<vPlugins: {self.__str__()}>"

    def __eq__(self, other):
        return isinstance(other, vPlugin) and self.__str__() == other.__str__()


class vPlugins(commands.Cog):
    """
    Plugins expand Modmail functionality by allowing third-party addons.

    These addons could have a range of features from moderation to simply
    making your life as a moderator easier!
    Learn how to create a plugin yourself here:
    https://github.com/kyb3r/modmail/wiki/Plugins
    """ # will translate l8r

    def __init__(self, bot):
        self.bot = bot
        self.registry = {}
        self.loaded_vplugins = set()
        self._ready_event = asyncio.Event()

        self.bot.loop.create_task(self.populate_registry())

        if self.bot.config.get("enable_plugins"):
            self.bot.loop.create_task(self.initial_load_vplugins())
        else:
            logger.info("Plugins not loaded since ENABLE_PLUGINS=false.")

    async def populate_registry(self):
        url = "https://vincysuper07.cf/bot/plugin.json"
        async with self.bot.session.get(url) as resp:
            self.registry = json.loads(await resp.text())

    async def initial_load_vplugins(self):
        await self.bot.wait_for_connected()

        for vplugin_name in list(self.bot.config["plugins"]):
            try:
                vplugin = vPlugin.from_string(vplugin_name, strict=True)
            except InvalidPluginError:
                self.bot.config["plugins"].remove(vplugin_name)
                try:
                    # For backwards compat
                    vplugin = vPlugin.from_string(vplugin_name)
                except InvalidPluginError:
                    logger.error("Failed to parse vplugin name: %s.", vplugin_name, exc_info=True)
                    continue

                logger.info("Migrated legacy plugin name: %s, now %s.", vplugin_name, str(vplugin))
                self.bot.config["plugins"].append(str(vplugin))

            try:
                await self.download_vplugin(vplugin)
                await self.load_vplugin(vplugin)
            except Exception:
                logger.error("Error when loading plugin %s.", vplugin, exc_info=True)
                continue

        logger.debug("Finished loading all plugins.")
        self._ready_event.set()
        await self.bot.config.update()

    async def download_vplugin(self, vplugin, force=False):
        if vplugin.abs_vpath.exists() and not force:
            return

        vplugin.abs_vpath.mkdir(parents=True, exist_ok=True)

        if vplugin.cache_vpath.exists() and not force:
            vplugin_io = vplugin.cache_vpath.open("rb")
            logger.debug("Loading cached %s.", vplugin.cache_vpath)

        else:
            async with self.bot.session.get(vplugin.url) as resp:
                logger.debug("Downloading %s.", vplugin.url)
                raw = await resp.read()
                vplugin_io = io.BytesIO(raw)
                if not vplugin.cache_vpath.parent.exists():
                    vplugin.cache_vpath.parent.mkdir(parents=True)

                with vplugin.cache_vpath.open("wb") as f:
                    f.write(raw)

        with zipfile.ZipFile(vplugin_io) as zipf:
            for info in zipf.infolist():
                vpath = PurePath(info.filename)
                if len(vpath.parts) >= 3 and vpath.parts[1] == vplugin.name:
                    vplugin_vpath = vplugin.abs_vpath / Path(*vpath.parts[2:])
                    if info.is_dir():
                        vplugin_vpath.mkdir(parents=True, exist_ok=True)
                    else:
                        vplugin_vpath.parent.mkdir(parents=True, exist_ok=True)
                        with zipf.open(info) as src, vplugin_vpath.open("wb") as dst:
                            shutil.copyfileobj(src, dst)

        vplugin_io.close()

    async def load_vplugin(self, vplugin):
        if not (vplugin.abs_vpath / f"{vplugin.name}.py").exists():
            raise InvalidPluginError(f"{vplugin.name}.py not found.")

        req_txt = vplugin.abs_vpath / "requirements.txt"

        if req_txt.exists():
            # Install PIP requirements

            venv = hasattr(sys, "real_prefix") or hasattr(sys, "base_prefix") # in a virtual env
            user_install = " --user" if not venv else ""
            proc = await asyncio.create_subprocess_shell(
                f"{sys.executable} -m pip install --upgrade{user_install} -r {req_txt} -q -q",
                stderr=PIPE,
                stdout=PIPE,
            )

            logger.debug("Downloading requirements for %s.", vplugin.ext_string)

            stdout, stderr = await proc.communicate()

            if stdout:
                logger.debug("[stdout]\n%s.", stdout.decode())

            if stderr:
                logger.debug("[stderr]\n%s.", stderr.decode())
                logger.error(
                    "Failed to download requirements for %s.", vplugin.ext_string, exc_info=True
                )
                raise InvalidPluginError(
                    f"Unable to download requirements: ```\n{stderr.decode()}\n```"
                )

            if os.vpath.exists(USER_SITE):
                sys.vpath.insert(0, USER_SITE)

        try:
            self.bot.load_extension(vplugin.ext_string)
            logger.info("Loaded vplugin: %s", vplugin.ext_string.split(".")[-1])
            self.loaded_vplugins.add(vplugin)

        except commands.ExtensionError as exc:
            logger.error("vplugin load failure: %s", vplugin.ext_string, exc_info=True)
            raise InvalidPluginError("Cannot load extension, plugin invalid.") from exc

    async def parse_user_input(self, ctx, vplugin_name, check_version=False):

        if not self._ready_event.is_set():
            embed = discord.Embed(
                description="vplugins are still loading, please try again later.",
                color=self.bot.main_color,
            )
            await ctx.send(embed=embed)
            return

        if vplugin_name in self.registry:
            details = self.registry[vplugin_name]
            user, repo = details["repository"].split("/", maxsplit=1)
            branch = details.get("branch")

            if check_version:
                required_version = details.get("bot_version", False)

                if required_version and self.bot.version < parse_version(required_version):
                    embed = discord.Embed(
                        description="Your bot's version is too low. "
                        f"This plugin requires version `{required_version}`.",
                        color=self.bot.error_color,
                    )
                    await ctx.send(embed=embed)
                    return

            vplugin = vPlugin(user, repo, vplugin_name, branch)

        else:
            try:
                vplugin = vPlugin.from_string(vplugin_name)
            except InvalidPluginError:
                embed = discord.Embed(
                    description="Invalid plugin name, double check the plugin name "
                    "or use one of the following formats: "
                    "username/repo/plugin, username/repo/plugin@branch.",
                    color=self.bot.error_color,
                )
                await ctx.send(embed=embed)
                return
        return vplugin

    @commands.group(aliases=["vplugin"], invoke_without_command=True)
    @checks.has_permissions(PermissionLevel.OWNER)
    async def vplugins(self, ctx):
        """
        Manage plugins for Modmail.
        """

        await ctx.send_help(ctx.command)

    @vplugins.command(name="add", aliases=["install", "load"])
    @checks.has_permissions(PermissionLevel.OWNER)
    @trigger_typing
    async def vplugins_add(self, ctx, *, vplugin_name: str):
        """
        Install a new vplugin for the bot.

        `vplugin_name` can be the name of the vplugin found in `{prefix}vplugin registry`,
        or a direct reference to a GitHub hosted vplugin (in the format `user/repo/name[@branch]`).
        """

        vplugin = await self.parse_user_input(ctx, vplugin_name, check_version=True)
        if vplugin is None:
            return

        if str(vplugin) in self.bot.config["plugins"]:
            embed = discord.Embed(
                description="This vplugin is already installed.", color=self.bot.error_color
            )
            return await ctx.send(embed=embed)

        if vplugin.name in self.bot.cogs:
            # another class with the same name
            embed = discord.Embed(
                description="Cannot install this vplugin (dupe cog name).",
                color=self.bot.error_color,
            )
            return await ctx.send(embed=embed)

        embed = discord.Embed(
            description=f"Starting to download vplugin from {vplugin.link}...",
            color=self.bot.main_color,
        )
        msg = await ctx.send(embed=embed)

        try:
            await self.download_vplugin(vplugin, force=True)
        except Exception:
            logger.warning("Unable to download vplugin %s.", vplugin, exc_info=True)

            embed = discord.Embed(
                description="Failed to download vplugin, check logs for error.",
                color=self.bot.error_color,
            )

            return await msg.edit(embed=embed)

        self.bot.config["plugins"].append(str(vplugin))
        await self.bot.config.update()

        if self.bot.config.get("enable_plugins"):

            invalidate_caches()

            try:
                await self.load_vplugin(vplugin)
            except Exception:
                logger.warning("Unable to load vplugin %s.", vplugin, exc_info=True)

                embed = discord.Embed(
                    description="Failed to download vplugin, check logs for error.",
                    color=self.bot.error_color,
                )

            else:
                embed = discord.Embed(
                    description="Successfully installed vplugin.\n"
                    "*Friendly reminder, vplugins have absolute control over your bot. "
                    "Please only install vplugins from developers you trust.*",
                    color=self.bot.main_color,
                )
        else:
            embed = discord.Embed(
                description="Successfully installed vplugin.\n"
                "*Friendly reminder, vplugins have absolute control over your bot. "
                "Please only install vplugins from developers you trust.*\n\n"
                "This vplugin is currently not enabled due to `ENABLE_vpluginS=false`, "
                "to re-enable vplugins, remove or change `ENABLE_vpluginS=true` and restart your bot.",
                color=self.bot.main_color,
            )
        return await msg.edit(embed=embed)

    @vplugins.command(name="remove", aliases=["del", "delete"])
    @checks.has_permissions(PermissionLevel.OWNER)
    async def vplugins_remove(self, ctx, *, vplugin_name: str):
        """
        Remove an installed vplugin of the bot.

        `vplugin_name` can be the name of the vplugin found in `{prefix}vplugin registry`, or a direct reference
        to a GitHub hosted vplugin (in the format `user/repo/name[@branch]`).
        """
        vplugin = await self.parse_user_input(ctx, vplugin_name)
        if vplugin is None:
            return

        if str(vplugin) not in self.bot.config["plugins"]:
            embed = discord.Embed(
                description="vplugin is not installed.", color=self.bot.error_color
            )
            return await ctx.send(embed=embed)

        if self.bot.config.get("enable_plugins"):
            try:
                self.bot.unload_extension(vplugin.ext_string)
                self.loaded_vplugins.remove(vplugin)
            except (commands.ExtensionNotLoaded, KeyError):
                logger.warning("vplugin was never loaded.")

        self.bot.config["plugins"].remove(str(vplugin))
        await self.bot.config.update()
        shutil.rmtree(
            vplugin.abs_vpath,
            onerror=lambda *args: logger.warning(
                "Failed to remove vplugin files %s: %s", vplugin, str(args[2])
            ),
        )
        try:
            vplugin.abs_vpath.parent.rmdir()
            vplugin.abs_vpath.parent.parent.rmdir()
        except OSError:
            pass  # dir not empty

        embed = discord.Embed(
            description="The vplugin is successfully uninstalled.", color=self.bot.main_color
        )
        await ctx.send(embed=embed)

    async def update_vplugin(self, ctx, vplugin_name):
        logger.debug("Updating %s.", vplugin_name)
        vplugin = await self.parse_user_input(ctx, vplugin_name, check_version=True)
        if vplugin is None:
            return

        if str(vplugin) not in self.bot.config["plugins"]:
            embed = discord.Embed(
                description="vplugin is not installed.", color=self.bot.error_color
            )
            return await ctx.send(embed=embed)

        async with ctx.typing():
            await self.download_vplugin(vplugin, force=True)
            if self.bot.config.get("enable_plugins"):
                try:
                    self.bot.unload_extension(vplugin.ext_string)
                except commands.ExtensionError:
                    logger.warning("vplugin unload fail.", exc_info=True)
                await self.load_vplugin(vplugin)
            logger.debug("Updated %s.", vplugin_name)
            embed = discord.Embed(
                description=f"Successfully updated {vplugin.name}.", color=self.bot.main_color
            )
            return await ctx.send(embed=embed)

    @vplugins.command(name="update")
    @checks.has_permissions(PermissionLevel.OWNER)
    async def vplugins_update(self, ctx, *, vplugin_name: str = None):
        """
        Update a plugin for the bot.

        `plugin_name` can be the name of the plugin found in `{prefix}plugin registry`, or a direct reference
        to a GitHub hosted plugin (in the format `user/repo/name[@branch]`).

        To update all plugins, do `{prefix}plugins update`.
        """

        if vplugin_name is None:
            # pylint: disable=redefined-argument-from-local
            for vplugin_name in self.bot.config["plugins"]:
                await self.update_vplugin(ctx, vplugin_name)
        else:
            await self.update_vplugin(ctx, vplugin_name)

    @vplugins.command(name="loaded", aliases=["enabled", "installed"])
    @checks.has_permissions(PermissionLevel.OWNER)
    async def vplugins_loaded(self, ctx):
        """
        Show a list of currently loaded vplugins.
        """

        if not self.bot.config.get("enable_plugins"):
            embed = discord.Embed(
                description="No plugins are loaded due to `ENABLE_PLUGINS=false`, "
                "to re-enable plugins, remove or set `ENABLE_PLUGINS=true` and restart your bot.",
                color=self.bot.error_color,
            )
            return await ctx.send(embed=embed)

        if not self._ready_event.is_set():
            embed = discord.Embed(
                description="Plugins are still loading, please try again later.",
                color=self.bot.main_color,
            )
            return await ctx.send(embed=embed)

        if not self.loaded_vplugins:
            embed = discord.Embed(
                description="There are no vplugins currently loaded.", color=self.bot.error_color
            )
            return await ctx.send(embed=embed)

        loaded_vplugins = map(str, sorted(self.loaded_vplugins))
        pages = ["```\n"]
        for vplugin in loaded_vplugins:
            msg = str(vplugin) + "\n"
            if len(msg) + len(pages[-1]) + 3 <= 2048:
                pages[-1] += msg
            else:
                pages[-1] += "```"
                pages.append(f"```\n{msg}")

        if pages[-1][-3:] != "```":
            pages[-1] += "```"

        embeds = []
        for page in pages:
            embed = discord.Embed(
                title="Loaded vplugins:", description=page, color=self.bot.main_color
            )
            embeds.append(embed)
        paginator = EmbedPaginatorSession(ctx, *embeds)
        await paginator.run()

    @vplugins.group(invoke_without_command=True, name="registry", aliases=["list", "info"])
    @checks.has_permissions(PermissionLevel.OWNER)
    async def vplugins_registry(self, ctx, *, vplugin_name: typing.Union[int, str] = None):
        """
        Shows a list of all approved plugins.

        Usage:
        `{prefix}plugin registry` Details about all plugins.
        `{prefix}plugin registry plugin-name` Details about the indicated plugin.
        `{prefix}plugin registry page-number` Jump to a page in the registry.
        """

        await self.populate_registry()

        embeds = []

        registry = sorted(self.registry.items(), key=lambda elem: elem[0])

        if isinstance(vplugin_name, int):
            index = vplugin_name - 1
            if index < 0:
                index = 0
            if index >= len(registry):
                index = len(registry) - 1
        else:
            index = next((i for i, (n, _) in enumerate(registry) if vplugin_name == n), 0)

        if not index and vplugin_name is not None:
            embed = discord.Embed(
                color=self.bot.error_color,
                description=f'Could not find a vplugin with name "{vplugin_name}" within the registry.',
            )

            matches = get_close_matches(vplugin_name, self.registry.keys())

            if matches:
                embed.add_field(
                    name="Perhaps you meant:", value="\n".join(f"`{m}`" for m in matches)
                )

            return await ctx.send(embed=embed)

        for name, details in registry:
            details = self.registry[name]
            user, repo = details["repository"].split("/", maxsplit=1)
            branch = details.get("branch")

            vplugin = vPlugin(user, repo, name, branch)

            embed = discord.Embed(
                color=self.bot.main_color,
                description=details["description"],
                url=vplugin.link,
                title=details["repository"],
            )

            embed.add_field(
                name="Installation", value=f"```{self.bot.prefix}vplugins add {name}```"
            )

            embed.set_author(
                name=details["title"], icon_url=details.get("icon_url"), url=vplugin.link
            )

            if details.get("thumbnail_url"):
                embed.set_thumbnail(url=details.get("thumbnail_url"))

            if details.get("image_url"):
                embed.set_image(url=details.get("image_url"))

            if vplugin in self.loaded_vplugins:
                embed.set_footer(text="This vplugin is currently loaded.")
            else:
                required_version = details.get("bot_version", False)
                if required_version and self.bot.version < parse_version(required_version):
                    embed.set_footer(
                        text="Your bot is unable to install this vplugin, "
                        f"minimum required version is v{required_version}."
                    )
                else:
                    embed.set_footer(text="Your bot is able to install this vplugin.")

            embeds.append(embed)

        paginator = EmbedPaginatorSession(ctx, *embeds)
        paginator.current = index
        await paginator.run()

    @vplugins_registry.command(name="compact", aliases=["slim"])
    @checks.has_permissions(PermissionLevel.OWNER)
    async def vplugins_registry_compact(self, ctx):
        """
        Shows a compact view of all vplugins within the registry.
        """

        await self.populate_registry()

        registry = sorted(self.registry.items(), key=lambda elem: elem[0])

        pages = [""]

        for vplugin_name, details in registry:
            details = self.registry[vplugin_name]
            user, repo = details["repository"].split("/", maxsplit=1)
            branch = details.get("branch")

            vplugin = vPlugin(user, repo, vplugin_name, branch)

            desc = discord.utils.escape_markdown(details["description"].replace("\n", ""))

            name = f"[`{vplugin.name}`]({vplugin.link})"
            fmt = f"{name} - {desc}"

            if vplugin_name in self.loaded_vplugins:
                limit = 75 - len(vplugin_name) - 4 - 8 + len(name)
                if limit < 0:
                    fmt = vplugin.name
                    limit = 75
                fmt = truncate(fmt, limit) + "[loaded]\n"
            else:
                limit = 75 - len(vplugin_name) - 4 + len(name)
                if limit < 0:
                    fmt = vplugin.name
                    limit = 75
                fmt = truncate(fmt, limit) + "\n"

            if len(fmt) + len(pages[-1]) <= 2048:
                pages[-1] += fmt
            else:
                pages.append(fmt)

        embeds = []

        for page in pages:
            embed = discord.Embed(color=self.bot.main_color, description=page)
            embed.set_author(name="vplugin Registry", icon_url=self.bot.user.avatar_url)
            embeds.append(embed)

        paginator = EmbedPaginatorSession(ctx, *embeds)
        await paginator.run()


def setup(bot):
    bot.add_cog(vPlugins(bot))
