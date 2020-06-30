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
from site import USER_SITE  # nosec
from subprocess import PIPE  # nosec

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


class Plugina:
    def __init__(self, user, repo, name, branch=None):
        self.user = user
        self.repo = repo
        self.name = name
        self.branch = branch if branch is not None else "master"
        self.url = f"https://github.com/{user}/{repo}/archive/{self.branch}.zip"
        self.link = f"https://github.com/{user}/{repo}/tree/{self.branch}/{name}"

    @property
    def path(self):
        return PurePath("plugins") / self.user / self.repo / f"{self.name}-{self.branch}"

    @property
    def abs_path(self):
        return Path(__file__).absolute().parent.parent / self.path

    @property
    def cache_path(self):
        return (
            Path(__file__).absolute().parent.parent
            / "temp"
            / "plugins-cache"
            / f"{self.user}-{self.repo}-{self.branch}.zip"
        )

    @property
    def ext_string(self):
        return f"plugins.{self.user}.{self.repo}.{self.name}-{self.branch}.{self.name}"

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
            return Plugina(*m.groups())
        raise InvalidPluginError("Cannot decipher %s.", s)  # pylint: disable=raising-format-tuple

    def __hash__(self):
        return hash((self.user, self.repo, self.name, self.branch))

    def __repr__(self):
        return f"<Plugins: {self.__str__()}>"

    def __eq__(self, other):
        return isinstance(other, Plugina) and self.__str__() == other.__str__()


class Plugin(commands.Cog):
    """
    I plugin espandono le funzionalità del VincyBot07 aggiungendo estensioni di terze parti.

    Questi addon potrebbero avere un numero di funzioni da moderazione a semplici
    rendendo la tua vita da moderatore più facile!
    Impara come creare il tuo plugin qui:
    https://github.com/kyb3r/modmail/wiki/Plugins
    """

    def __init__(self, bot):
        self.bot = bot
        self.registry = {}
        self.loaded_plugins = set()
        self._ready_event = asyncio.Event()

        self.bot.loop.create_task(self.populate_registry())
        self.bot.loop.create_task(self.populate_vregistry())

        if self.bot.config.get("enable_plugins"):
            self.bot.loop.create_task(self.initial_load_plugins())
        else:
            logger.info("Plugins not loaded since ENABLE_PLUGINS=false.")

    async def populate_registry(self):
        url = "https://raw.githubusercontent.com/kyb3r/modmail/master/plugins/registry.json"
        async with self.bot.session.get(url) as resp:
            self.registry = json.loads(await resp.text())

    async def populate_vregistry(self):
        vurl = "http://vincysuper07.cf/bot/plugin.json"
        async with self.bot.session.get(vurl) as resp:
            self.registry = json.loads(await resp.text())

    async def initial_load_plugins(self):
        await self.bot.wait_for_connected()

        for plugin_name in list(self.bot.config["plugins"]):
            try:
                plugin = Plugina.from_string(plugin_name, strict=True)
            except InvalidPluginError:
                self.bot.config["plugins"].remove(plugin_name)
                try:
                    # For backwards compat
                    plugin = Plugina.from_string(plugin_name)
                except InvalidPluginError:
                    logger.error("Failed to parse plugin name: %s.", plugin_name, exc_info=True)
                    continue

        for vplugin_name in list(self.bot.config["plugins"]):
            try:
                plugin = Plugina.from_string(vplugin_name, strict=True)
            except InvalidPluginError:
                self.bot.config["plugins"].remove(vplugin_name)
                try:
                    # For backwards compat
                    plugin = Plugina.from_string(vplugin_name)
                except InvalidPluginError:
                    logger.error("Failed to parse plugin name: %s.", vplugin_name, exc_info=True)
                    continue

                logger.info("Migrated legacy plugin name: %s, now %s.", vplugin_name, str(plugin))
                self.bot.config["plugins"].append(str(plugin))

            try:
                await self.download_plugin(plugin)
                await self.load_plugin(plugin)
            except Exception:
                logger.error("Non è stato possibile caricare %s.", plugin, exc_info=True)
                continue

        logger.debug("Ho finito di caricare tutti i plugin.")
        self._ready_event.set()
        await self.bot.config.update()

    async def download_plugin(self, plugin, force=False):
        if plugin.abs_path.exists() and not force:
            return

        plugin.abs_path.mkdir(parents=True, exist_ok=True)

        if plugin.cache_path.exists() and not force:
            plugin_io = plugin.cache_path.open("rb")
            logger.debug("Sto caricando %s dalla cache.", plugin.cache_path)

        else:
            async with self.bot.session.get(plugin.url) as resp:
                logger.debug("Sto scaricando %s.", plugin.url)
                raw = await resp.read()
                plugin_io = io.BytesIO(raw)
                if not plugin.cache_path.parent.exists():
                    plugin.cache_path.parent.mkdir(parents=True)

                with plugin.cache_path.open("wb") as f:
                    f.write(raw)

        with zipfile.ZipFile(plugin_io) as zipf:
            for info in zipf.infolist():
                path = PurePath(info.filename)
                if len(path.parts) >= 3 and path.parts[1] == plugin.name:
                    plugin_path = plugin.abs_path / Path(*path.parts[2:])
                    if info.is_dir():
                        plugin_path.mkdir(parents=True, exist_ok=True)
                    else:
                        plugin_path.parent.mkdir(parents=True, exist_ok=True)
                        with zipf.open(info) as src, plugin_path.open("wb") as dst:
                            shutil.copyfileobj(src, dst)

        plugin_io.close()

    async def load_plugin(self, plugin):
        if not (plugin.abs_path / f"{plugin.name}.py").exists():
            raise InvalidPluginError(f"{plugin.name}.py not found.")

        req_txt = plugin.abs_path / "requirements.txt"

        if req_txt.exists():
            # Install PIP requirements

            venv = hasattr(sys, "real_prefix") or hasattr(sys, "base_prefix")  # in a virtual env
            user_install = " --user" if not venv else ""
            proc = await asyncio.create_subprocess_shell(
                f"{sys.executable} -m pip install --upgrade{user_install} -r {req_txt} -q -q",
                stderr=PIPE,
                stdout=PIPE,
            )

            logger.debug("Sto scaricando i requisiti per %s.", plugin.ext_string)

            stdout, stderr = await proc.communicate()

            if stdout:
                logger.debug("[stdout]\n%s.", stdout.decode())

            if stderr:
                logger.debug("[stderr]\n%s.", stderr.decode())
                logger.error(
                    "Non è stato possibile scaricare i requisiti per %s.",
                    plugin.ext_string,
                    exc_info=True,
                )
                raise InvalidPluginError(
                    f"Non è stato possibile scaricare i requisiti: ```\n{stderr.decode()}\n```"
                )

            if os.path.exists(USER_SITE):
                sys.path.insert(0, USER_SITE)

        try:
            self.bot.load_extension(plugin.ext_string)
            logger.info('Il plugin "%s" è stato caricato.', plugin.ext_string.split(".")[-1])
            self.loaded_plugins.add(plugin)

        except commands.ExtensionError as exc:
            logger.error(
                "Non è stato possibile caricare il plugin: %s", plugin.ext_string, exc_info=True
            )
            raise InvalidPluginError(
                "Non è stato possibile caricare l'estensione, il plugin non è valido."
            ) from exc

    async def parse_user_input(self, ctx, plugin_name, check_version=False):

        if not self._ready_event.is_set():
            embed = discord.Embed(
                description="Sto ancora caricando alcuni plugin, per favore, riprova più tardi.",
                color=self.bot.main_color,
            )
            await ctx.send(embed=embed)
            return

        if plugin_name in self.registry:
            details = self.registry[plugin_name]
            user, repo = details["repository"].split("/", maxsplit=1)
            branch = details.get("branch")

            if check_version:
                required_version = details.get("bot_version", False)

                if required_version and self.bot.version < parse_version(required_version):
                    embed = discord.Embed(
                        description="La versione del bot è troppo bassa. "
                        f"Questo plugin richiede la versione `{required_version}`.",
                        color=self.bot.error_color,
                    )
                    await ctx.send(embed=embed)
                    return

            plugin = Plugina(user, repo, plugin_name, branch)

        else:
            try:
                plugin = Plugina.from_string(plugin_name)
            except InvalidPluginError:
                embed = discord.Embed(
                    description="Il nome del plugin non è valido, ricontrolla il nome del plugin "
                    "oppure usa uno dei seguenti formati: "
                    "username/repo/plugin, username/repo/plugin@branch.\n"
                    f"(nota da Vincysuper07: prova a fare prima `{ctx.prefix}vplugins registry` "
                    "e riprova per aggiungere/rimuovere un plugin dal mio database.)",
                    color=self.bot.error_color,
                )
                await ctx.send(embed=embed)
                return
        return plugin

    @commands.group(aliases=["plugin"], invoke_without_command=True)
    @checks.has_permissions(PermissionLevel.OWNER)
    async def plugins(self, ctx):
        """
        Manage plugins for Modmail.
        """

        await ctx.send_help(ctx.command)

    @plugins.command(name="add", aliases=["install", "load"])
    @checks.has_permissions(PermissionLevel.OWNER)
    @trigger_typing
    async def plugins_add(self, ctx, *, plugin_name: str):
        """
        Install a new plugin for the bot.

        `plugin_name` can be the name of the plugin found in `{prefix}plugin registry`,
        or a direct reference to a GitHub hosted plugin (in the format `user/repo/name[@branch]`).
        """

        plugin = await self.parse_user_input(ctx, plugin_name, check_version=True)
        if plugin is None:
            return

        if str(plugin) in self.bot.config["plugins"]:
            embed = discord.Embed(
                description="This plugin is already installed.", color=self.bot.error_color
            )
            return await ctx.send(embed=embed)

        if plugin.name in self.bot.cogs:
            # another class with the same name
            embed = discord.Embed(
                description="Cannot install this plugin (dupe cog name).",
                color=self.bot.error_color,
            )
            return await ctx.send(embed=embed)

        embed = discord.Embed(
            description=f"Starting to download plugin from {plugin.link}...",
            color=self.bot.main_color,
        )
        msg = await ctx.send(embed=embed)

        try:
            await self.download_plugin(plugin, force=True)
        except Exception:
            logger.warning("Unable to download plugin %s.", plugin, exc_info=True)

            embed = discord.Embed(
                description="Failed to download plugin, check logs for error.",
                color=self.bot.error_color,
            )

            return await msg.edit(embed=embed)

        self.bot.config["plugins"].append(str(plugin))
        await self.bot.config.update()

        if self.bot.config.get("enable_plugins"):

            invalidate_caches()

            try:
                await self.load_plugin(plugin)
            except Exception:
                logger.warning("Unable to load plugin %s.", plugin, exc_info=True)

                embed = discord.Embed(
                    description="Failed to download plugin, check logs for error.",
                    color=self.bot.error_color,
                )

            else:
                embed = discord.Embed(
                    description="Successfully installed plugin.\n"
                    "*Friendly reminder, plugins have absolute control over your bot. "
                    "Please only install plugins from developers you trust.*",
                    color=self.bot.main_color,
                )
        else:
            embed = discord.Embed(
                description="Successfully installed plugin.\n"
                "*Friendly reminder, plugins have absolute control over your bot. "
                "Please only install plugins from developers you trust.*\n\n"
                "This plugin is currently not enabled due to `ENABLE_PLUGINS=false`, "
                "to re-enable plugins, remove or change `ENABLE_PLUGINS=true` and restart your bot.",
                color=self.bot.main_color,
            )
        return await msg.edit(embed=embed)

    @plugins.command(name="remove", aliases=["del", "delete"])
    @checks.has_permissions(PermissionLevel.OWNER)
    async def plugins_remove(self, ctx, *, plugin_name: str):
        """
        Remove an installed plugin of the bot.

        `plugin_name` can be the name of the plugin found in `{prefix}plugin registry`, or a direct reference
        to a GitHub hosted plugin (in the format `user/repo/name[@branch]`).
        """
        plugin = await self.parse_user_input(ctx, plugin_name)
        if plugin is None:
            return

        if str(plugin) not in self.bot.config["plugins"]:
            embed = discord.Embed(
                description="Plugin is not installed.", color=self.bot.error_color
            )
            return await ctx.send(embed=embed)

        if self.bot.config.get("enable_plugins"):
            try:
                self.bot.unload_extension(plugin.ext_string)
                self.loaded_plugins.remove(plugin)
            except (commands.ExtensionNotLoaded, KeyError):
                logger.warning("Plugin was never loaded.")

        self.bot.config["plugins"].remove(str(plugin))
        await self.bot.config.update()
        shutil.rmtree(
            plugin.abs_path,
            onerror=lambda *args: logger.warning(
                "Failed to remove plugin files %s: %s", plugin, str(args[2])
            ),
        )
        try:
            plugin.abs_path.parent.rmdir()
            plugin.abs_path.parent.parent.rmdir()
        except OSError:
            pass  # dir not empty

        embed = discord.Embed(
            description="The plugin is successfully uninstalled.", color=self.bot.main_color
        )
        await ctx.send(embed=embed)

    async def update_plugin(self, ctx, plugin_name):
        logger.debug("Updating %s.", plugin_name)
        plugin = await self.parse_user_input(ctx, plugin_name, check_version=True)
        if plugin is None:
            return

        if str(plugin) not in self.bot.config["plugins"]:
            embed = discord.Embed(
                description="Plugin is not installed.", color=self.bot.error_color
            )
            return await ctx.send(embed=embed)

        async with ctx.typing():
            await self.download_plugin(plugin, force=True)
            if self.bot.config.get("enable_plugins"):
                try:
                    self.bot.unload_extension(plugin.ext_string)
                except commands.ExtensionError:
                    logger.warning("Plugin unload fail.", exc_info=True)
                await self.load_plugin(plugin)
            logger.debug("Updated %s.", plugin_name)
            embed = discord.Embed(
                description=f"Successfully updated {plugin.name}.", color=self.bot.main_color
            )
            return await ctx.send(embed=embed)

    @plugins.command(name="update")
    @checks.has_permissions(PermissionLevel.OWNER)
    async def plugins_update(self, ctx, *, plugin_name: str = None):
        """
        Update a plugin for the bot.

        `plugin_name` can be the name of the plugin found in `{prefix}plugin registry`, or a direct reference
        to a GitHub hosted plugin (in the format `user/repo/name[@branch]`).

        To update all plugins, do `{prefix}plugins update`.
        """

        if plugin_name is None:
            # pylint: disable=redefined-argument-from-local
            for plugin_name in self.bot.config["plugins"]:
                await self.update_plugin(ctx, plugin_name)
        else:
            await self.update_plugin(ctx, plugin_name)

    @plugins.command(name="loaded", aliases=["enabled", "installed"])
    @checks.has_permissions(PermissionLevel.OWNER)
    async def plugins_loaded(self, ctx):
        """
        Show a list of currently loaded plugins.
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

        if not self.loaded_plugins:
            embed = discord.Embed(
                description="There are no plugins currently loaded.", color=self.bot.error_color
            )
            return await ctx.send(embed=embed)

        loaded_plugins = map(str, sorted(self.loaded_plugins))
        pages = ["```\n"]
        for plugin in loaded_plugins:
            msg = str(plugin) + "\n"
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
                title="Loaded plugins:", description=page, color=self.bot.main_color
            )
            embeds.append(embed)
        paginator = EmbedPaginatorSession(ctx, *embeds)
        await paginator.run()

    @plugins.group(invoke_without_command=True, name="registry", aliases=["list", "info"])
    @checks.has_permissions(PermissionLevel.OWNER)
    async def plugins_registry(self, ctx, *, plugin_name: typing.Union[int, str] = None):
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

        if isinstance(plugin_name, int):
            index = plugin_name - 1
            if index < 0:
                index = 0
            if index >= len(registry):
                index = len(registry) - 1
        else:
            index = next((i for i, (n, _) in enumerate(registry) if plugin_name == n), 0)

        if not index and plugin_name is not None:
            embed = discord.Embed(
                color=self.bot.error_color,
                description=f'Could not find a plugin with name "{plugin_name}" within the registry.',
            )

            matches = get_close_matches(plugin_name, self.registry.keys())

            if matches:
                embed.add_field(
                    name="Perhaps you meant:", value="\n".join(f"`{m}`" for m in matches)
                )

            return await ctx.send(embed=embed)

        for name, details in registry:
            details = self.registry[name]
            user, repo = details["repository"].split("/", maxsplit=1)
            branch = details.get("branch")

            plugin = Plugina(user, repo, name, branch)

            embed = discord.Embed(
                color=self.bot.main_color,
                description=details["description"],
                url=plugin.link,
                title=details["repository"],
            )

            embed.add_field(
                name="Installation", value=f"```{self.bot.prefix}plugins add {name}```"
            )

            embed.set_author(
                name=details["title"], icon_url=details.get("icon_url"), url=plugin.link
            )

            if details.get("thumbnail_url"):
                embed.set_thumbnail(url=details.get("thumbnail_url"))

            if details.get("image_url"):
                embed.set_image(url=details.get("image_url"))

            if plugin in self.loaded_plugins:
                embed.set_footer(text="This plugin is currently loaded.")
            else:
                required_version = details.get("bot_version", False)
                if required_version and self.bot.version < parse_version(required_version):
                    embed.set_footer(
                        text="Your bot is unable to install this plugin, "
                        f"minimum required version is v{required_version}."
                    )
                else:
                    embed.set_footer(text="Your bot is able to install this plugin.")

            embeds.append(embed)

        paginator = EmbedPaginatorSession(ctx, *embeds)
        paginator.current = index
        await paginator.run()

    @plugins_registry.command(name="compact", aliases=["slim"])
    @checks.has_permissions(PermissionLevel.OWNER)
    async def plugins_registry_compact(self, ctx):
        """
        Shows a compact view of all plugins within the registry.
        """

        await self.populate_registry()

        registry = sorted(self.registry.items(), key=lambda elem: elem[0])

        pages = [""]

        for plugin_name, details in registry:
            details = self.registry[plugin_name]
            user, repo = details["repository"].split("/", maxsplit=1)
            branch = details.get("branch")

            plugin = Plugina(user, repo, plugin_name, branch)

            desc = discord.utils.escape_markdown(details["description"].replace("\n", ""))

            name = f"[`{plugin.name}`]({plugin.link})"
            fmt = f"{name} - {desc}"

            if plugin_name in self.loaded_plugins:
                limit = 75 - len(plugin_name) - 4 - 8 + len(name)
                if limit < 0:
                    fmt = plugin.name
                    limit = 75
                fmt = truncate(fmt, limit) + "[loaded]\n"
            else:
                limit = 75 - len(plugin_name) - 4 + len(name)
                if limit < 0:
                    fmt = plugin.name
                    limit = 75
                fmt = truncate(fmt, limit) + "\n"

            if len(fmt) + len(pages[-1]) <= 2048:
                pages[-1] += fmt
            else:
                pages.append(fmt)

        embeds = []

        for page in pages:
            embed = discord.Embed(color=self.bot.main_color, description=page)
            embed.set_author(name="Plugin Registry", icon_url=self.bot.user.avatar_url)
            embeds.append(embed)

        paginator = EmbedPaginatorSession(ctx, *embeds)
        await paginator.run()

    # vplugins inizia qui
    @commands.group(aliases=["vplugin"], invoke_without_command=True)
    @checks.has_permissions(PermissionLevel.OWNER)
    async def vplugins(self, ctx):
        """
       Questo è il database plugin fatto da Vincy.
        """

        await ctx.send_help(ctx.command)

    @vplugins.command(name="add", aliases=["install", "load"])
    @checks.has_permissions(PermissionLevel.OWNER)
    @trigger_typing
    async def vplugins_add(self, ctx, *, vplugin_name: str):
        """
        Installa un nuovo plugin per il bot.
        """

        plugin = await self.parse_user_input(ctx, vplugin_name, check_version=True)
        if plugin is None:
            return

        if str(plugin) in self.bot.config["plugins"]:
            embed = discord.Embed(
                description="Questo plugin è già installato.", color=self.bot.error_color
            )
            return await ctx.send(embed=embed)

        if plugin.name in self.bot.cogs:
            # another class with the same name
            embed = discord.Embed(
                description="Non è possibile installare questo plugin (c'è un altro plugin con lo stesso nome).",
                color=self.bot.error_color,
            )
            return await ctx.send(embed=embed)

        embed = discord.Embed(
            description=f"Sto scaricando il plugin da {plugin.link}...", color=self.bot.main_color,
        )
        msg = await ctx.send(embed=embed)

        try:
            await self.download_plugin(plugin, force=True)
        except Exception:
            logger.warning("Impossibile scaricare il plugin %s.", plugin, exc_info=True)

            embed = discord.Embed(
                description="Non è stato possibile scaricare il plugin, controlla i log per l'errore.",
                color=self.bot.error_color,
            )

            return await msg.edit(embed=embed)

        self.bot.config["plugins"].append(str(plugin))
        await self.bot.config.update()

        if self.bot.config.get("enable_plugins"):

            invalidate_caches()

            try:
                await self.load_plugin(plugin)
            except Exception:
                logger.warning(
                    "Non è stato possibile caricare il plugin %s.", plugin, exc_info=True
                )

                embed = discord.Embed(
                    description="Non è stato possibile scaricare il plugin, controlla i log per l'errore.",
                    color=self.bot.error_color,
                )

            else:
                embed = discord.Embed(
                    description="Il plugin è stato installato!", color=self.bot.main_color,
                )
        else:
            embed = discord.Embed(
                description="Il plugin è stato installato!\n"
                "I plugin non verranno caricati per via della configurazione `ENABLE_PLUGINS` impostata a `false`, "
                "per ri-abilitare i plugin, cambia quella riga a `ENABLE_PLUGINS=true` oppure rimuovila e riavvia il bot.",
                color=self.bot.main_color,
            )
        return await msg.edit(embed=embed)

    @vplugins.command(name="remove", aliases=["del", "delete"])
    @checks.has_permissions(PermissionLevel.OWNER)
    async def vplugins_remove(self, ctx, *, vplugin_name: str):
        """
        Rimuove un plugin installato.
        """
        plugin = await self.parse_user_input(ctx, vplugin_name)
        if plugin is None:
            return

        if str(plugin) not in self.bot.config["plugins"]:
            embed = discord.Embed(
                description="Quel plugin non era installato.", color=self.bot.error_color
            )
            return await ctx.send(embed=embed)

        if self.bot.config.get("enable_plugins"):
            try:
                self.bot.unload_extension(plugin.ext_string)
                self.loaded_plugins.remove(plugin)
            except (commands.ExtensionNotLoaded, KeyError):
                logger.warning("Il plugin %s non è mai stato caricato.", plugin)

        self.bot.config["plugins"].remove(str(plugin))
        await self.bot.config.update()
        shutil.rmtree(
            plugin.abs_path,
            onerror=lambda *args: logger.warning(
                "Non è stato possibile rimuovere i file del plugin %s: %s", plugin, str(args[2])
            ),
        )
        try:
            plugin.abs_path.parent.rmdir()
            plugin.abs_path.parent.parent.rmdir()
        except OSError:
            pass  # dir not empty

        embed = discord.Embed(
            description="Il plugin è stato disinstallato.", color=self.bot.main_color
        )
        await ctx.send(embed=embed)

    async def update_plugin(self, ctx, vplugin_name):
        logger.debug("Sto aggiornando %s.", vplugin_name)
        plugin = await self.parse_user_input(ctx, vplugin_name, check_version=True)
        if plugin is None:
            return

        if str(plugin) not in self.bot.config["plugins"]:
            embed = discord.Embed(
                description="Quel plugin non è installato.", color=self.bot.error_color
            )
            return await ctx.send(embed=embed)

        async with ctx.typing():
            await self.download_plugin(plugin, force=True)
            if self.bot.config.get("enable_plugins"):
                try:
                    self.bot.unload_extension(plugin.ext_string)
                except commands.ExtensionError:
                    logger.warning("Plugin unload fail.", exc_info=True)
                await self.load_plugin(plugin)
            logger.debug("Il plugin %s è stato aggiornato.", vplugin_name)
            embed = discord.Embed(
                description=f"Il plugin {plugin.name} è stato aggiornato.",
                color=self.bot.main_color,
            )
            return await ctx.send(embed=embed)

    @vplugins.command(name="update")
    @checks.has_permissions(PermissionLevel.OWNER)
    async def vplugins_update(self, ctx, *, vplugin_name: str = None):
        """
        Aggiorna un plugin del bot.
        Per aggiornare tutti i plugin, usa solo `{prefix}vplugins update`.
        """

        if vplugin_name is None:
            # pylint: disable=redefined-argument-from-local
            for vplugin_name in self.bot.config["plugins"]:
                await self.update_plugin(ctx, vplugin_name)
        else:
            await self.update_plugin(ctx, vplugin_name)

    @vplugins.command(name="loaded", aliases=["enabled", "installed"])
    @checks.has_permissions(PermissionLevel.OWNER)
    async def vplugins_loaded(self, ctx):
        """
        Mostra una lista di plugin caricati.
        """

        if not self.bot.config.get("enable_plugins"):
            embed = discord.Embed(
                description="Nessun plugin è stato caricato per via della configurazione `ENABLE_PLUGINS` impostata a `false`, "
                "per ri-abilitare i plugin, cambia quella linea `ENABLE_PLUGINS=true` oppure eliminala e riavvia il bot.",
                color=self.bot.error_color,
            )
            return await ctx.send(embed=embed)

        if not self._ready_event.is_set():
            embed = discord.Embed(
                description="Sto ancora caricando dei plugin, riprova più tardi.",
                color=self.bot.main_color,
            )
            return await ctx.send(embed=embed)

        if not self.loaded_plugins:
            embed = discord.Embed(
                description="Al momento nessun plugin è stato caricato.",
                color=self.bot.error_color,
            )
            return await ctx.send(embed=embed)

        loaded_plugins = map(str, sorted(self.loaded_plugins))
        pages = ["```\n"]
        for plugin in loaded_plugins:
            msg = str(plugin) + "\n"
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
                title="Plugin abilitati:", description=page, color=self.bot.main_color
            )
            embeds.append(embed)
        paginator = EmbedPaginatorSession(ctx, *embeds)
        await paginator.run()

    @vplugins.group(invoke_without_command=True, name="registry", aliases=["list", "info"])
    @checks.has_permissions(PermissionLevel.OWNER)
    async def vplugins_registry(self, ctx, *, vplugin_name: typing.Union[int, str] = None):
        """
        Mostra una lista di plugin del database di Vincy.

        Utilizzo:
        `{prefix}plugin registry` Dettagli riguardo tutti i plugin.
        `{prefix}plugin registry nome-plugin` Dettagli riguardo il plugin che hai specificato.
        `{prefix}plugin registry numero-pagina` Vai ad una pagina del registro.
        """

        await self.populate_vregistry()

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
                description=f'Non è stato trovato alcun plugin col nome "{vplugin_name}" nel registro.',
            )

            matches = get_close_matches(vplugin_name, self.registry.keys())

            if matches:
                embed.add_field(
                    name="Forse intendevi:", value="\n".join(f"`{m}`" for m in matches)
                )

            return await ctx.send(embed=embed)

        for name, details in registry:
            details = self.registry[name]
            user, repo = details["repository"].split("/", maxsplit=1)
            branch = details.get("branch")

            plugin = Plugina(user, repo, name, branch)

            embed = discord.Embed(
                color=self.bot.main_color,
                description=details["description"],
                url=plugin.link,
                title=details["repository"],
            )

            embed.add_field(
                name="Installazione", value=f"```{self.bot.prefix}vplugins add {name}```"
            )

            embed.set_author(
                name=details["title"], icon_url=details.get("icon_url"), url=plugin.link
            )

            if details.get("thumbnail_url"):
                embed.set_thumbnail(url=details.get("thumbnail_url"))

            if details.get("image_url"):
                embed.set_image(url=details.get("image_url"))

            if plugin in self.loaded_plugins:
                embed.set_footer(text="Questo plugin è già caricato.")
            else:
                required_version = details.get("bot_version", False)
                if required_version and self.bot.version < parse_version(required_version):
                    embed.set_footer(
                        text="Il bot non può installare questo plugin, "
                        f"la versione minima richiesta è v{required_version}."
                    )
                else:
                    embed.set_footer(text="Il tuo bot può installare questo plugin.")

            embeds.append(embed)

        paginator = EmbedPaginatorSession(ctx, *embeds)
        paginator.current = index
        await paginator.run()

    @vplugins_registry.command(name="compact", aliases=["slim"])
    @checks.has_permissions(PermissionLevel.OWNER)
    async def vplugins_registry_compact(self, ctx):
        """
        Mostra una versione compatta del registro.
        """

        await self.populate_vregistry()

        registry = sorted(self.registry.items(), key=lambda elem: elem[0])

        pages = [""]

        for vplugin_name, details in registry:
            details = self.registry[vplugin_name]
            user, repo = details["repository"].split("/", maxsplit=1)
            branch = details.get("branch")

            plugin = Plugina(user, repo, vplugin_name, branch)

            desc = discord.utils.escape_markdown(details["description"].replace("\n", ""))

            name = f"[`{plugin.name}`]({plugin.link})"
            fmt = f"{name} - {desc}"

            if vplugin_name in self.loaded_plugins:
                limit = 75 - len(vplugin_name) - 4 - 8 + len(name)
                if limit < 0:
                    fmt = plugin.name
                    limit = 75
                fmt = truncate(fmt, limit) + "[loaded]\n"
            else:
                limit = 75 - len(vplugin_name) - 4 + len(name)
                if limit < 0:
                    fmt = plugin.name
                    limit = 75
                fmt = truncate(fmt, limit) + "\n"

            if len(fmt) + len(pages[-1]) <= 2048:
                pages[-1] += fmt
            else:
                pages.append(fmt)

        embeds = []

        for page in pages:
            embed = discord.Embed(color=self.bot.main_color, description=page)
            embed.set_author(name="Registro plugin", icon_url=self.bot.user.avatar_url)
            embeds.append(embed)

        paginator = EmbedPaginatorSession(ctx, *embeds)
        await paginator.run()


def setup(bot):
    bot.add_cog(Plugin(bot))
