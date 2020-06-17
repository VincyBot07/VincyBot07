import asyncio
import re
from datetime import datetime
from itertools import zip_longest
from typing import Optional, Union
from types import SimpleNamespace

import discord
from discord.ext import commands
from discord.utils import escape_markdown

from dateutil import parser
from natural.date import duration

from core import checks
from core.models import PermissionLevel, getLogger
from core.paginator import EmbedPaginatorSession
from core.thread import Thread
from core.time import UserFriendlyTime, human_timedelta
from core.utils import *

logger = getLogger(__name__)


class Modmail(commands.Cog):
    """Comandi direttamente relativi alle funzionalita' di Modmail."""

    def __init__(self, bot):
        self.bot = bot

    @commands.command()
    @trigger_typing
    @checks.has_permissions(PermissionLevel.OWNER)
    async def setup(self, ctx):
        """
        Imposta un server per Modmail.

        Devi avviare il comando solo una
        volta dopo aver configurato Modmail.
        """

        if ctx.guild != self.bot.modmail_guild:
            return await ctx.send(
                f"Puoi solo impostare il bot nel seguente server: {self.bot.modmail_guild}."
            )

        if self.bot.main_category is not None:
            logger.debug("Non posso reimpostare il server, main_category e' trovato.")
            return await ctx.send(f"{self.bot.modmail_guild} e' gia' impostato.")

        if self.bot.modmail_guild is None:
            embed = discord.Embed(
                title="Errore",
                description="Server funzionante Modmail non trovato.",
                color=self.bot.error_color,
            )
            return await ctx.send(embed=embed)

        overwrites = {
            self.bot.modmail_guild.default_role: discord.PermissionOverwrite(read_messages=False),
            self.bot.modmail_guild.me: discord.PermissionOverwrite(read_messages=True),
        }

        for level in PermissionLevel:
            if level <= PermissionLevel.REGULAR:
                continue
            permissions = self.bot.config["level_permissions"].get(level.name, [])
            for perm in permissions:
                perm = int(perm)
                if perm == -1:
                    key = self.bot.modmail_guild.default_role
                else:
                    key = self.bot.modmail_guild.get_member(perm)
                    if key is None:
                        key = self.bot.modmail_guild.get_role(perm)
                if key is not None:
                    logger.info("Accesso alla categoria Modmail %s consentito.", key.name)
                    overwrites[key] = discord.PermissionOverwrite(read_messages=True)

        category = await self.bot.modmail_guild.create_category(
            name="Modmail", overwrites=overwrites
        )

        await category.edit(position=0)

        log_channel = await self.bot.modmail_guild.create_text_channel(
            name="bot-logs", category=category
        )

        embed = discord.Embed(
            title="Ricordo amichevole",
            description=f"Potresti usare il comando `{self.bot.prefix}config set log_channel_id "
            "<id canale>` per impostare un canale personalizzato per i log, poi puoi eliminare questo canale log "
            f"chiamato {log_channel.mention}.",
            color=self.bot.main_color,
        )

        embed.add_field(
            name="Grazie per aver usato il nostro bot!",
            value="Se ti piace cio' che vedo, considera di dare alla "
            "[repo una stella](https://github.com/kyb3r/modmail) :star: e se ti senti cosi' "
            "generoso, compraci del caffe' [donando qua](https://streamelements.com/nicoloilsuper/tip) :heart:!",
        )

        embed.set_footer(text=f'Digita "{self.bot.prefix}help" per una lista completa di comandi.')
        await log_channel.send(embed=embed)

        self.bot.config["main_category_id"] = category.id
        self.bot.config["log_channel_id"] = log_channel.id

        await self.bot.config.update()
        await ctx.send(
            "**Server impostato con successo.**\n"
            "Considera di impostare livelli di permesso per dare accesso a ruoli "
            "o utenti alle funzionalita' di Modmail.\n\n"
            f"Digita:\n- `{self.bot.prefix}permissions` e `{self.bot.prefix}permissions add` "
            "per piu' informazioni nell'impostare permessi.\n"
            f"- `{self.bot.prefix}config help` per una lista di personalizzazioni utili."
        )

        if not self.bot.config["command_permissions"] and not self.bot.config["level_permissions"]:
            await self.bot.update_perms(PermissionLevel.REGULAR, -1)
            for owner_ids in self.bot.owner_ids:
                await self.bot.update_perms(PermissionLevel.OWNER, owner_ids)

    @commands.group(aliases=["snippets"], invoke_without_command=True)
    @checks.has_permissions(PermissionLevel.SUPPORTER)
    async def snippet(self, ctx, *, name: str.lower = None):
        """
        Crea messaggi pre-definiti per l'uso sui thread.

        Quando `{prefix}snippet` e' usato da se' stesso, questo trovera'
        una lista di snippet che sono correntemente impostati. `{prefix}nome-snippet` mostrera' cio' a cui lo snippet
        punta.

        Per creare uno snippet:
        - `{prefix}snippet add nome-snippet Un testo pre-definito.`

        Puoi usare quello snippet in un canale thread
        con `{prefix}nome-snippet`, il messaggio "Un testo pre-definito."
        verra' inviato al recipiente.

        Al momento, non c'e' un comando snippet anonimo precostruito; comunque, una soluzione
        e' disponibile: si usa `{prefix}alias`. Ecco come:
        - `{prefix}alias add nome-snippet anonreply Un testo anonimo pre-definito.`

        Vedi anche `{prefix}alias`.
        """

        if name is not None:
            val = self.bot.snippets.get(name)
            if val is None:
                embed = create_not_found_embed(name, self.bot.snippets.keys(), "Snippet")
            else:
                embed = discord.Embed(
                    title=f'Snippet - "{name}":', description=val, color=self.bot.main_color
                )
            return await ctx.send(embed=embed)

        if not self.bot.snippets:
            embed = discord.Embed(
                color=self.bot.error_color, description="Non hai nessuno snippet al momento."
            )
            embed.set_footer(
                text=f'Controlla "{self.bot.prefix}help snippet add" per aggiungere uno snippet.'
            )
            embed.set_author(name="Snippet", icon_url=ctx.guild.icon_url)
            return await ctx.send(embed=embed)

        embeds = []

        for i, names in enumerate(zip_longest(*(iter(sorted(self.bot.snippets)),) * 15)):
            description = format_description(i, names)
            embed = discord.Embed(color=self.bot.main_color, description=description)
            embed.set_author(name="Snippet", icon_url=ctx.guild.icon_url)
            embeds.append(embed)

        session = EmbedPaginatorSession(ctx, *embeds)
        await session.run()

    @snippet.command(name="raw")
    @checks.has_permissions(PermissionLevel.SUPPORTER)
    async def snippet_raw(self, ctx, *, name: str.lower):
        """
        Vedi il contenuto intero di uno snippet.
        """
        val = self.bot.snippets.get(name)
        if val is None:
            embed = create_not_found_embed(name, self.bot.snippets.keys(), "Snippet")
        else:
            val = truncate(escape_code_block(val), 2048 - 7)
            embed = discord.Embed(
                title=f'Snippet intero - "{name}":',
                description=f"```\n{val}```",
                color=self.bot.main_color,
            )

        return await ctx.send(embed=embed)

    @snippet.command(name="add")
    @checks.has_permissions(PermissionLevel.SUPPORTER)
    async def snippet_add(self, ctx, name: str.lower, *, value: commands.clean_content):
        """
        Aggiungi uno snippet.

        Semplicemente per aggiungere uno snippet, fai: ```
        {prefix}snippet add hey ciao a tutti :)
        ```
        poi quando digiti `{prefix}hey`, "ciao a tutti :)" verra' inviato al recipiente.

        Per aggiungere uno snippet multi-parola, usa le virgolette: ```
        {prefix}snippet add "due parole" questo e' uno snippet di due parole.
        ```
        """
        if name in self.bot.snippets:
            embed = discord.Embed(
                title="Errore",
                color=self.bot.error_color,
                description=f"Lo snippet `{name}` esiste gia'.",
            )
            return await ctx.send(embed=embed)

        if name in self.bot.aliases:
            embed = discord.Embed(
                title="Errore",
                color=self.bot.error_color,
                description=f"Un alias che condivide lo stesso nome esiste: `{name}`.",
            )
            return await ctx.send(embed=embed)

        if len(name) > 120:
            embed = discord.Embed(
                title="Errore",
                color=self.bot.error_color,
                description="I nomi degli snippet non possono essere superiori ai 120 caratteri.",
            )
            return await ctx.send(embed=embed)

        self.bot.snippets[name] = value
        await self.bot.config.update()

        embed = discord.Embed(
            title="Snippet aggiunto",
            color=self.bot.main_color,
            description="Snippet creato con successo.",
        )
        return await ctx.send(embed=embed)

    @snippet.command(name="remove", aliases=["del", "delete"])
    @checks.has_permissions(PermissionLevel.SUPPORTER)
    async def snippet_remove(self, ctx, *, name: str.lower):
        """Rimuovi uno snippet."""

        if name in self.bot.snippets:
            embed = discord.Embed(
                title="Snippet rimosso",
                color=self.bot.main_color,
                description=f"Lo snippet `{name}` e' ora stato eliminato.",
            )
            self.bot.snippets.pop(name)
            await self.bot.config.update()
        else:
            embed = create_not_found_embed(name, self.bot.snippets.keys(), "Snippet")
        await ctx.send(embed=embed)

    @snippet.command(name="edit")
    @checks.has_permissions(PermissionLevel.SUPPORTER)
    async def snippet_edit(self, ctx, name: str.lower, *, value):
        """
        Modifica uno snippet.

        Per modificare un nome di snippet multi-parola, usa le virgolette: ```
        {prefix}snippet edit "due parole" questo e' un nuovo snippet a due parole.
        ```
        """
        if name in self.bot.snippets:
            self.bot.snippets[name] = value
            await self.bot.config.update()

            embed = discord.Embed(
                title="Snippet modificato",
                color=self.bot.main_color,
                description=f'`{name}` ora inviera\' "{value}".',
            )
        else:
            embed = create_not_found_embed(name, self.bot.snippets.keys(), "Snippet")
        await ctx.send(embed=embed)

    @commands.command()
    @checks.has_permissions(PermissionLevel.MODERATOR)
    @checks.thread_only()
    async def move(self, ctx, category: discord.CategoryChannel, *, specifics: str = None):
        """
        Sposta un thread in una nuova categoria.

        `category` puo' essere un nome, una menzione o un ID di categoria.
        `specifics` e' una stringa che spiega come eseguire lo spostamento. Es: "silenziosamente"
        """
        thread = ctx.thread
        silent = False

        if specifics:
            silent_words = ["silenzioso", "silenziosamente"]
            silent = any(word in silent_words for word in specifics.split())

        await thread.channel.edit(category=category, sync_permissions=True)

        if self.bot.config["thread_move_notify"] and not silent:
            embed = discord.Embed(
                title="Thread Spostato",
                description=self.bot.config["thread_move_response"],
                color=self.bot.main_color,
            )
            await thread.recipient.send(embed=embed)

        sent_emoji, _ = await self.bot.retrieve_emoji()
        await self.bot.add_reaction(ctx.message, sent_emoji)

    async def send_scheduled_close_message(self, ctx, after, silent=False):
        human_delta = human_timedelta(after.dt)

        silent = "*silenziosamente* " if silent else ""

        embed = discord.Embed(
            title="Chiusura programmata",
            description=f"Questo thread si chiudera' {silent}in {human_delta}.",
            color=self.bot.error_color,
        )

        if after.arg and not silent:
            embed.add_field(name="Messaggio", value=after.arg)

        embed.set_footer(
            text="La chiusura verra' cancellata se un messaggio sul thread e' inviato."
        )
        embed.timestamp = after.dt

        await ctx.send(embed=embed)

    @commands.command(usage="[after] [close message]")
    @checks.has_permissions(PermissionLevel.SUPPORTER)
    @checks.thread_only()
    async def close(self, ctx, *, after: UserFriendlyTime = None):
        """
        Chiudi il thread corrente.

        Chiudi dopo un periodo di tempo:
        - `{prefix}close in 5 hours`
        - `{prefix}close 2m30s`

        Messaggi di chiusura personalizzati:
        - `{prefix}close 2 hours Il problema e' stato risolto.`
        - `{prefix}close Ti contattiamo quando troveremo di piu'.`

        Chiudi un thread silenziosamente (niente messaggio)
        - `{prefix}close silently`
        - `{prefix}close in 10m silently`

        Previeni che il thread venga chiuso:
        - `{prefix}close cancel`
        """

        thread = ctx.thread

        now = datetime.utcnow()

        close_after = (after.dt - now).total_seconds() if after else 0
        message = after.arg if after else None
        silent = str(message).lower() in {"silent", "silently"}
        cancel = str(message).lower() == "cancel"

        if cancel:

            if thread.close_task is not None or thread.auto_close_task is not None:
                await thread.cancel_closure(all=True)
                embed = discord.Embed(
                    color=self.bot.error_color,
                    description="La chiusura programmata e' stata appena cancellata.",
                )
            else:
                embed = discord.Embed(
                    color=self.bot.error_color,
                    description="Il thread non era schedulato per essere chiuso.",
                )

            return await ctx.send(embed=embed)

        if after and after.dt > now:
            await self.send_scheduled_close_message(ctx, after, silent)

        await thread.close(closer=ctx.author, after=close_after, message=message, silent=silent)

    @staticmethod
    def parse_user_or_role(ctx, user_or_role):
        mention = None
        if user_or_role is None:
            mention = ctx.author.mention
        elif hasattr(user_or_role, "mention"):
            mention = user_or_role.mention
        elif user_or_role in {"here", "everyone", "@here", "@everyone"}:
            mention = "@" + user_or_role.lstrip("@")
        return mention

    @commands.command(aliases=["alert"])
    @checks.has_permissions(PermissionLevel.SUPPORTER)
    @checks.thread_only()
    async def notify(
        self, ctx, *, user_or_role: Union[discord.Role, User, str.lower, None] = None
    ):
        """
        Notifica un utente o ruolo quando il prossimo messaggio del thread sara' ricevuto.

        Una volta che il messaggio del thread e' ricevuto, `user_or_role` sara' pingato solo una volta.

        Lascia `user_or_role` vuoto per notificare te stesso.
        `@here` e `@everyone` puo' essere sostituito con `here` ed `everyone`.
        `user_or_role` puo' essere un ID utente, menzione, nome. ID ruolo, menzione, nome, "everyone" o "here".
        """
        mention = self.parse_user_or_role(ctx, user_or_role)
        if mention is None:
            raise commands.BadArgument(f"{user_or_role} non e' un valido utente o ruolo.")

        thread = ctx.thread

        if str(thread.id) not in self.bot.config["notification_squad"]:
            self.bot.config["notification_squad"][str(thread.id)] = []

        mentions = self.bot.config["notification_squad"][str(thread.id)]

        if mention in mentions:
            embed = discord.Embed(
                color=self.bot.error_color,
                description=f"{mention} e' gia' pronto per essere menzionato.",
            )
        else:
            mentions.append(mention)
            await self.bot.config.update()
            embed = discord.Embed(
                color=self.bot.main_color,
                description=f"{mention} verra' menzionato al prossimo messaggio ricevuto.",
            )
        return await ctx.send(embed=embed)

    @commands.command(aliases=["unalert"])
    @checks.has_permissions(PermissionLevel.SUPPORTER)
    @checks.thread_only()
    async def unnotify(
        self, ctx, *, user_or_role: Union[discord.Role, User, str.lower, None] = None
    ):
        """
        Togli la notifica da un utente, ruolo, o da te stesso da un thread.

        Lascia `user_or_role` vuoto per togliere la notifica a te stesso.
        `@here` e `@everyone` puo' essere sostituito con `here` ed `everyone`.
        `user_or_role` puo' essere un ID utente, menzione, nome. ID ruolo, menzione, nome, "everyone" o "here".
        """
        mention = self.parse_user_or_role(ctx, user_or_role)
        if mention is None:
            mention = f"`{user_or_role}`"

        thread = ctx.thread

        if str(thread.id) not in self.bot.config["notification_squad"]:
            self.bot.config["notification_squad"][str(thread.id)] = []

        mentions = self.bot.config["notification_squad"][str(thread.id)]

        if mention not in mentions:
            embed = discord.Embed(
                color=self.bot.error_color,
                description=f"{mention} non ha una notifica in attesa.",
            )
        else:
            mentions.remove(mention)
            await self.bot.config.update()
            embed = discord.Embed(
                color=self.bot.main_color, description=f"{mention} non sara' piu' notificato."
            )
        return await ctx.send(embed=embed)

    @commands.command(aliases=["sub"])
    @checks.has_permissions(PermissionLevel.SUPPORTER)
    @checks.thread_only()
    async def subscribe(
        self, ctx, *, user_or_role: Union[discord.Role, User, str.lower, None] = None
    ):
        """
        Notifica un utente, un ruolo, o te stesso per ogni messaggio del thread ricevuto.

        Verrai pingato per ogni messaggio del thread ricevuto fin quando non ti disiscrivi.

        Lascia `user_or_role` vuoto per iscriverti.
        `@here` e `@everyone` puo' essere sostituito con `here` ed `everyone`.
        `user_or_role` puo' essere un ID utente, menzione, nome. ID ruolo, menzione, nome, "everyone" o "here".
        """
        mention = self.parse_user_or_role(ctx, user_or_role)
        if mention is None:
            raise commands.BadArgument(f"{user_or_role} non e' un valido utente o ruolo.")

        thread = ctx.thread

        if str(thread.id) not in self.bot.config["subscriptions"]:
            self.bot.config["subscriptions"][str(thread.id)] = []

        mentions = self.bot.config["subscriptions"][str(thread.id)]

        if mention in mentions:
            embed = discord.Embed(
                color=self.bot.error_color,
                description=f"{mention} si e' gia' iscritto a questo thread.",
            )
        else:
            mentions.append(mention)
            await self.bot.config.update()
            embed = discord.Embed(
                color=self.bot.main_color,
                description=f"{mention} verra' notificato di ogni messaggio nel thread.",
            )
        return await ctx.send(embed=embed)

    @commands.command(aliases=["unsub"])
    @checks.has_permissions(PermissionLevel.SUPPORTER)
    @checks.thread_only()
    async def unsubscribe(
        self, ctx, *, user_or_role: Union[discord.Role, User, str.lower, None] = None
    ):
        """
        Disiscrivi un utente, ruolo, o te stesso da un thread.

        Lascia `user_or_role` vuoto per disiscriverti.
        `@here` e `@everyone` puo' essere sostituito con `here` ed `everyone`.
        `user_or_role` puo' essere un ID utente, menzione, nome. ID ruolo, menzione, nome, "everyone" o "here".
        """
        mention = self.parse_user_or_role(ctx, user_or_role)
        if mention is None:
            mention = f"`{user_or_role}`"

        thread = ctx.thread

        if str(thread.id) not in self.bot.config["subscriptions"]:
            self.bot.config["subscriptions"][str(thread.id)] = []

        mentions = self.bot.config["subscriptions"][str(thread.id)]

        if mention not in mentions:
            embed = discord.Embed(
                color=self.bot.error_color,
                description=f"{mention} non si e' gia' iscritto a questo thread.",
            )
        else:
            mentions.remove(mention)
            await self.bot.config.update()
            embed = discord.Embed(
                color=self.bot.main_color,
                description=f"{mention} si e' ora disiscritto da questo thread.",
            )
        return await ctx.send(embed=embed)

    @commands.command()
    @checks.has_permissions(PermissionLevel.SUPPORTER)
    @checks.thread_only()
    async def nsfw(self, ctx):
        """Contrassegna un thread Modmail come NSFW."""
        await ctx.channel.edit(nsfw=True)
        sent_emoji, _ = await self.bot.retrieve_emoji()
        await self.bot.add_reaction(ctx.message, sent_emoji)

    @commands.command()
    @checks.has_permissions(PermissionLevel.SUPPORTER)
    @checks.thread_only()
    async def sfw(self, ctx):
        """Contrassegna un thread Modmail come SFW."""
        await ctx.channel.edit(nsfw=False)
        sent_emoji, _ = await self.bot.retrieve_emoji()
        await self.bot.add_reaction(ctx.message, sent_emoji)

    @commands.command()
    @checks.has_permissions(PermissionLevel.SUPPORTER)
    @checks.thread_only()
    async def loglink(self, ctx):
        """Ritrova il link ai log del thread corrente."""
        log_link = await self.bot.api.get_log_link(ctx.channel.id)
        await ctx.send(embed=discord.Embed(color=self.bot.main_color, description=log_link))

    def format_log_embeds(self, logs, avatar_url):
        embeds = []
        logs = tuple(logs)
        title = f"Risultati in totale trovati ({len(logs)})"

        for entry in logs:
            created_at = parser.parse(entry["created_at"])

            prefix = self.bot.config["log_url_prefix"].strip("/")
            if prefix == "NONE":
                prefix = ""
            log_url = f"{self.bot.config['log_url'].strip('/')}{'/' + prefix if prefix else ''}/{entry['key']}"

            username = entry["recipient"]["name"] + "#"
            username += entry["recipient"]["discriminator"]

            embed = discord.Embed(color=self.bot.main_color, timestamp=created_at)
            embed.set_author(name=f"{title} - {username}", icon_url=avatar_url, url=log_url)
            embed.url = log_url
            embed.add_field(name="Creato", value=duration(created_at, now=datetime.utcnow()))
            closer = entry.get("closer")
            if closer is None:
                closer_msg = "Sconosciuto"
            else:
                closer_msg = f"<@{closer['id']}>"
            embed.add_field(name="Chiuso da", value=closer_msg)

            if entry["recipient"]["id"] != entry["creator"]["id"]:
                embed.add_field(name="Creato da", value=f"<@{entry['creator']['id']}>")

            embed.add_field(name="Preview", value=format_preview(entry["messages"]), inline=False)

            if closer is not None:
                # BUG: Currently, logviewer can't display logs without a closer.
                embed.add_field(name="Link", value=log_url)
            else:
                logger.debug("Ingresso log errato: nessun chiudente.")
                embed.add_field(name="Chiave Log", value=f"`{entry['key']}`")

            embed.set_footer(text="ID recipiente: " + str(entry["recipient"]["id"]))
            embeds.append(embed)
        return embeds

    @commands.group(invoke_without_command=True)
    @checks.has_permissions(PermissionLevel.SUPPORTER)
    async def logs(self, ctx, *, user: User = None):
        """
        Ottieni log su un thread precedente Modmail di un recipiente.

        Lascia `user` vuoto quando questo comando e' usato dentro un
        canale del thread per mostrare log per il recipiente corrente.
        `user` puo' essere un ID utente, menzione, o nome.
        """

        await ctx.trigger_typing()

        if not user:
            thread = ctx.thread
            if not thread:
                raise commands.MissingRequiredArgument(SimpleNamespace(name="member"))
            user = thread.recipient

        default_avatar = "https://cdn.discordapp.com/embed/avatars/0.png"
        icon_url = getattr(user, "avatar_url", default_avatar)

        logs = await self.bot.api.get_user_logs(user.id)

        if not any(not log["open"] for log in logs):
            embed = discord.Embed(
                color=self.bot.error_color,
                description="Questo utente non ha nessun log precedente.",
            )
            return await ctx.send(embed=embed)

        logs = reversed([log for log in logs if not log["open"]])

        embeds = self.format_log_embeds(logs, avatar_url=icon_url)

        session = EmbedPaginatorSession(ctx, *embeds)
        await session.run()

    @logs.command(name="closed-by", aliases=["closeby"])
    @checks.has_permissions(PermissionLevel.SUPPORTER)
    async def logs_closed_by(self, ctx, *, user: User = None):
        """
        Ottieni tutti i log di chiusura dall'utente specificato.

        Se nessun `user` e' fornito, l'utente sara' la persona che ha inviato questo comando.
        `user` puo' essere un ID utente, menzione, o nome.
        """
        user = user if user is not None else ctx.author

        query = {"guild_id": str(self.bot.guild_id), "open": False, "closer.id": str(user.id)}

        projection = {"messages": {"$slice": 5}}

        entries = await self.bot.db.logs.find(query, projection).to_list(None)

        embeds = self.format_log_embeds(entries, avatar_url=self.bot.guild.icon_url)

        if not embeds:
            embed = discord.Embed(
                color=self.bot.error_color,
                description="Nessun ingresso log e' stato trovato per quella ricerca.",
            )
            return await ctx.send(embed=embed)

        session = EmbedPaginatorSession(ctx, *embeds)
        await session.run()

    @logs.command(name="delete", aliases=["wipe"])
    @checks.has_permissions(PermissionLevel.OWNER)
    async def logs_delete(self, ctx, key_or_link: str):
        """
        Elimina un ingresso del log dal database.
        """
        key = key_or_link.split("/")[-1]

        success = await self.bot.api.delete_log_entry(key)

        if not success:
            embed = discord.Embed(
                title="Errore",
                description=f"L'ingresso log `{key}` non e' stato trovato.",
                color=self.bot.error_color,
            )
        else:
            embed = discord.Embed(
                title="Successo",
                description=f"L'ingresso log `{key}` e' stato eliminato con successo.",
                color=self.bot.main_color,
            )

        await ctx.send(embed=embed)

    @logs.command(name="responded")
    @checks.has_permissions(PermissionLevel.SUPPORTER)
    async def logs_responded(self, ctx, *, user: User = None):
        """
        Ottieni tutti i log dove l'utente specificato ha risposto almeno una volta.

        Se nessun `user` e' fornito, l'utente sara' la persona che ha inviato questo comando.
        `user` puo' essere un ID utente, menzione, o nome.
        """
        user = user if user is not None else ctx.author

        entries = await self.bot.api.get_responded_logs(user.id)

        embeds = self.format_log_embeds(entries, avatar_url=self.bot.guild.icon_url)

        if not embeds:
            embed = discord.Embed(
                color=self.bot.error_color,
                description=f"{getattr(user, 'mention', user.id)} non ha risposto a nessun thread.",
            )
            return await ctx.send(embed=embed)

        session = EmbedPaginatorSession(ctx, *embeds)
        await session.run()

    @logs.command(name="search", aliases=["find"])
    @checks.has_permissions(PermissionLevel.SUPPORTER)
    async def logs_search(self, ctx, limit: Optional[int] = None, *, query):
        """
        Trova tutti i log che contengono risultati con la tua query.

        Fornisci un `limit` per specificare il massimo numero di log che il bot deve trovare.
        """

        await ctx.trigger_typing()

        query = {
            "guild_id": str(self.bot.guild_id),
            "open": False,
            "$text": {"$search": f'"{query}"'},
        }

        projection = {"messages": {"$slice": 5}}

        entries = await self.bot.db.logs.find(query, projection).to_list(limit)

        embeds = self.format_log_embeds(entries, avatar_url=self.bot.guild.icon_url)

        if not embeds:
            embed = discord.Embed(
                color=self.bot.error_color,
                description="Nessun ingresso log e' stato trovato per quella ricerca.",
            )
            return await ctx.send(embed=embed)

        session = EmbedPaginatorSession(ctx, *embeds)
        await session.run()

    @commands.command()
    @checks.has_permissions(PermissionLevel.SUPPORTER)
    @checks.thread_only()
    async def reply(self, ctx, *, msg: str = ""):
        """
        Rispondi a un thread Modmail.

        Supporta allegati e immagini cosi' come
        integrare automaticamente URL alle immagini.
        """
        ctx.message.content = msg
        async with ctx.typing():
            await ctx.thread.reply(ctx.message)

    @commands.command(aliases=["formatreply"])
    @checks.has_permissions(PermissionLevel.SUPPORTER)
    @checks.thread_only()
    async def freply(self, ctx, *, msg: str = ""):
        """
        Rispondi a un thread Modmail con variabili.

        Funziona proprio come `{prefix}reply`, pero' con l'aggiunta di tre variabili:
          - `{{channel}}` - l'oggetto `discord.TextChannel`
          - `{{recipient}}` - l'oggetto `discord.User` del recipiente
          - `{{author}}` - l'oggetto `discord.User` dell'autore

        Supporta allegati e immagini cosi' come
        integrare automaticamente URL alle immagini.
        """
        msg = self.bot.formatter.format(
            msg, channel=ctx.channel, recipient=ctx.thread.recipient, author=ctx.message.author
        )
        ctx.message.content = msg
        async with ctx.typing():
            await ctx.thread.reply(ctx.message)

    @commands.command(aliases=["anonreply", "anonymousreply"])
    @checks.has_permissions(PermissionLevel.SUPPORTER)
    @checks.thread_only()
    async def areply(self, ctx, *, msg: str = ""):
        """
        Rispondi a un thread in modo anonimo.

        Puoi editare il nome, l'avatar e il tag
        dell'utente anonimo usando il comando config.

        Modifica le variabili di configurazione `anon_username`,
        `anon_avatar_url` e `anon_tag` per fare cio'.
        """
        ctx.message.content = msg
        async with ctx.typing():
            await ctx.thread.reply(ctx.message, anonymous=True)

    @commands.command()
    @checks.has_permissions(PermissionLevel.SUPPORTER)
    @checks.thread_only()
    async def note(self, ctx, *, msg: str = ""):
        """
        Prendi una nota col contenuto del messaggio corrente.

        Utile per notare contesto.
        """
        ctx.message.content = msg
        async with ctx.typing():
            msg = await ctx.thread.note(ctx.message)
            await msg.pin()

    @commands.command()
    @checks.has_permissions(PermissionLevel.SUPPORTER)
    @checks.thread_only()
    async def edit(self, ctx, message_id: Optional[int] = None, *, message: str):
        """
        Modifica un messaggio inviato usando il comando reply o anonreply.

        Se nessun `message_id` e' fornito,
        l'ultimo messaggio inviato da uno staff verra' modificato.

        Nota: gli allegati **non possono** essere modificati.
        """
        thread = ctx.thread

        try:
            await thread.edit_message(message_id, message)
        except ValueError:
            return await ctx.send(
                embed=discord.Embed(
                    title="Fallito",
                    description="Non e' possibile trovare un messaggio da modificare.",
                    color=self.bot.error_color,
                )
            )

        sent_emoji, _ = await self.bot.retrieve_emoji()
        await self.bot.add_reaction(ctx.message, sent_emoji)

    @commands.command()
    @checks.has_permissions(PermissionLevel.SUPPORTER)
    async def contact(
        self,
        ctx,
        user: Union[discord.Member, discord.User],
        *,
        category: discord.CategoryChannel = None,
    ):
        """
        Crea un thread con un membro specificato.

        Se `category` e' specificato, il thread
        verra' creato in quella categoria specificata.

        `category`, se specificato, puo' essere un ID categoria, menzione, o nome.
        `user` puo' essere un ID utente, menzione, o nome.
        """

        if user.bot:
            embed = discord.Embed(
                color=self.bot.error_color,
                description="Non si puo' iniziare un thread con un bot.",
            )
            return await ctx.send(embed=embed)

        exists = await self.bot.threads.find(recipient=user)
        if exists:
            embed = discord.Embed(
                color=self.bot.error_color,
                description="Un thread per questo utente esiste "
                f"gia' su {exists.channel.mention}.",
            )
            await ctx.channel.send(embed=embed)

        else:
            thread = await self.bot.threads.create(user, creator=ctx.author, category=category)
            if self.bot.config["dm_disabled"] >= 1:
                logger.info("Contatto l'utente %s quando il modmail DM e' disattivato.", user)

            embed = discord.Embed(
                title="Thread creato",
                description=f"Thread iniziato da {ctx.author.mention} per {user.mention}.",
                color=self.bot.main_color,
            )
            await thread.wait_until_ready()
            await thread.channel.send(embed=embed)
            sent_emoji, _ = await self.bot.retrieve_emoji()
            await self.bot.add_reaction(ctx.message, sent_emoji)
            await asyncio.sleep(3)
            await ctx.message.delete()

    @commands.group(invoke_without_command=True)
    @checks.has_permissions(PermissionLevel.MODERATOR)
    @trigger_typing
    async def blocked(self, ctx):
        """Ottieni una lista di utenti bloccati."""

        embeds = [
            discord.Embed(title="Utenti bloccati", color=self.bot.main_color, description="")
        ]

        users = []

        for id_, reason in self.bot.blocked_users.items():
            user = self.bot.get_user(int(id_))
            if user:
                users.append((user.mention, reason))
            else:
                try:
                    user = await self.bot.fetch_user(id_)
                    users.append((user.mention, reason))
                except discord.NotFound:
                    users.append((id_, reason))

        if users:
            embed = embeds[0]

            for mention, reason in users:
                line = mention + f" - {reason or 'Nessuna ragione specificata'}\n"
                if len(embed.description) + len(line) > 2048:
                    embed = discord.Embed(
                        title="Utenti bloccati (Continua)",
                        color=self.bot.main_color,
                        description=line,
                    )
                    embeds.append(embed)
                else:
                    embed.description += line
        else:
            embeds[0].description = "Al momento non c'e' nessun utente bloccato."

        session = EmbedPaginatorSession(ctx, *embeds)
        await session.run()

    @blocked.command(name="whitelist")
    @checks.has_permissions(PermissionLevel.MODERATOR)
    @trigger_typing
    async def blocked_whitelist(self, ctx, *, user: User = None):
        """
        Whitelista o togli dalla whitelist un utente dall'essere bloccato.

        Utile per prevenire agli utenti di essere bloccati da restrizioni account_age/guild_age.
        """
        if user is None:
            thread = ctx.thread
            if thread:
                user = thread.recipient
            else:
                return await ctx.send_help(ctx.command)

        mention = getattr(user, "mention", f"`{user.id}`")
        msg = ""

        if str(user.id) in self.bot.blocked_whitelisted_users:
            embed = discord.Embed(
                title="Successo",
                description=f"{mention} non e' piu' whitelistato.",
                color=self.bot.main_color,
            )
            self.bot.blocked_whitelisted_users.remove(str(user.id))
            return await ctx.send(embed=embed)

        self.bot.blocked_whitelisted_users.append(str(user.id))

        if str(user.id) in self.bot.blocked_users:
            msg = self.bot.blocked_users.get(str(user.id)) or ""
            self.bot.blocked_users.pop(str(user.id))

        await self.bot.config.update()

        if msg.startswith("Messaggio di sistema: "):
            # Se un utente viene bloccato internamente (per esempio: sotto eta' minima account)
            # Mostra un messaggio esteso col messaggio interno dentro
            reason = msg[16:].strip().rstrip(".")
            embed = discord.Embed(
                title="Successo",
                description=f"{mention} era precedentemente bloccato internamente per "
                f'"{reason}". {mention} e\' ora whitelistato.',
                color=self.bot.main_color,
            )
        else:
            embed = discord.Embed(
                title="Successo",
                color=self.bot.main_color,
                description=f"{mention} e' ora whitelistato.",
            )

        return await ctx.send(embed=embed)

    @commands.command(usage="[user] [duration] [reason]")
    @checks.has_permissions(PermissionLevel.MODERATOR)
    @trigger_typing
    async def block(self, ctx, user: Optional[User] = None, *, after: UserFriendlyTime = None):
        """
        Blocca un utente dall'uso del Modmail.

        Potrai scegliere di impostare un tempo per quando l'utente sara' sbloccato automaticamente.

        Lascia `user` vuoto quando questo comando e' usato dentro a
        un canale del thread per bloccare il recipiente corrente.
        `user` puo' essere un ID utente, menzione, o nome.
        `duration` puo' essere un testo temporaneo "leggibile dagli umani" semplice. Vedi `{prefix}help close` per esempi.
        """

        if user is None:
            thread = ctx.thread
            if thread:
                user = thread.recipient
            elif after is None:
                raise commands.MissingRequiredArgument(SimpleNamespace(name="user"))
            else:
                raise commands.BadArgument(f'Non riesco a trovare l\'utente "{after.arg}".')

        mention = getattr(user, "mention", f"`{user.id}`")

        if str(user.id) in self.bot.blocked_whitelisted_users:
            embed = discord.Embed(
                title="Errore",
                description=f"Non e' possibile bloccare {mention}, l'utente e' whitelistato.",
                color=self.bot.error_color,
            )
            return await ctx.send(embed=embed)

        reason = f"by {escape_markdown(ctx.author.name)}#{ctx.author.discriminator}"

        if after is not None:
            if "%" in reason:
                raise commands.BadArgument('La ragione contiene il carattere illegale "%".')
            if after.arg:
                reason += f" for `{after.arg}`"
            if after.dt > after.now:
                reason += f" until {after.dt.isoformat()}"

        reason += "."

        msg = self.bot.blocked_users.get(str(user.id))
        if msg is None:
            msg = ""

        if str(user.id) in self.bot.blocked_users and msg:
            old_reason = msg.strip().rstrip(".")
            embed = discord.Embed(
                title="Successo",
                description=f"{mention} era bloccato precedentemente per {old_reason}.\n"
                f"{mention} e' ora bloccato per {reason}",
                color=self.bot.main_color,
            )
        else:
            embed = discord.Embed(
                title="Success",
                color=self.bot.main_color,
                description=f"{mention} e' ora bloccato per {reason}",
            )
        self.bot.blocked_users[str(user.id)] = reason
        await self.bot.config.update()

        return await ctx.send(embed=embed)

    @commands.command()
    @checks.has_permissions(PermissionLevel.MODERATOR)
    @trigger_typing
    async def unblock(self, ctx, *, user: User = None):
        """
        Sblocca un utente dall'usare Modmail.

        Lascia `user` vuoto quando questo comando e' usato dentro un
        canale del thread per sbloccare il recipiente corrente.
        `user` puo' essere un ID utente, menzione, o nome.
        """

        if user is None:
            thread = ctx.thread
            if thread:
                user = thread.recipient
            else:
                raise commands.MissingRequiredArgument(SimpleNamespace(name="user"))

        mention = getattr(user, "mention", f"`{user.id}`")
        name = getattr(user, "name", f"`{user.id}`")

        if str(user.id) in self.bot.blocked_users:
            msg = self.bot.blocked_users.pop(str(user.id)) or ""
            await self.bot.config.update()

            if msg.startswith("Messaggio di sistema: "):
                # Se un utente viene bloccato internamente (per esempio: sotto eta' minima account)
                # Mostra un messaggio esteso col messaggio interno dentro
                reason = msg[16:].strip().rstrip(".") or "nessuna ragione"
                embed = discord.Embed(
                    title="Successo",
                    description=f"{mention} era stato precedentemente bloccato internamente per {reason}.\n"
                    f"{mention} non e' piu' bloccato.",
                    color=self.bot.main_color,
                )
                embed.set_footer(
                    text="Comunque, se la stessa ragione del sistema viene applicata di nuovo, "
                    f"{name} sara' automaticamente bloccato di nuovo. "
                    f'Usa "{self.bot.prefix}blocked whitelist {user.id}" per whitelistare l\'utente.'
                )
            else:
                embed = discord.Embed(
                    title="Successo",
                    color=self.bot.main_color,
                    description=f"{mention} non e' piu' bloccato.",
                )
        else:
            embed = discord.Embed(
                title="Errore",
                description=f"{mention} non e' bloccato.",
                color=self.bot.error_color,
            )

        return await ctx.send(embed=embed)

    @commands.command()
    @checks.has_permissions(PermissionLevel.SUPPORTER)
    @checks.thread_only()
    async def delete(self, ctx, message_id: int = None):
        """
        Elimina un messaggio inviato in precedenza col comando reply oppure una nota.

        Elimina il precedente messaggio, finche' un ID messaggio viene fornito,
        che in tal caso, elimina il messaggio con quell'ID messaggio.

        Le note possono solo essere eliminate quando un ID nota e' fornito.
        """
        thread = ctx.thread

        try:
            await thread.delete_message(message_id, note=True)
        except ValueError as e:
            logger.warning("Fallimento nell'eliminazione del messaggio: %s.", e)
            return await ctx.send(
                embed=discord.Embed(
                    title="Fallito",
                    description="Non posso trovare un messaggio da eliminare.",
                    color=self.bot.error_color,
                )
            )

        sent_emoji, _ = await self.bot.retrieve_emoji()
        await self.bot.add_reaction(ctx.message, sent_emoji)

    @commands.command()
    @checks.has_permissions(PermissionLevel.SUPPORTER)
    async def repair(self, ctx):
        """
        Ripara un thread rovinato da Discord.
        """
        sent_emoji, blocked_emoji = await self.bot.retrieve_emoji()

        if ctx.thread:
            user_id = match_user_id(ctx.channel.topic)
            if user_id == -1:
                logger.info("Impostando il canale del topic corrente a User ID.")
                await ctx.channel.edit(topic=f"User ID: {ctx.thread.id}")
            return await self.bot.add_reaction(ctx.message, sent_emoji)

        logger.info("Tentando di sistemare il thread rovinato %s.", ctx.channel.name)

        # Cerca la cache per il canale
        user_id, thread = next(
            ((k, v) for k, v in self.bot.threads.cache.items() if v.channel == ctx.channel),
            (-1, None),
        )
        if thread is not None:
            logger.debug("Trovato thread con ID tamperato.")
            await ctx.channel.edit(
                reason="Sistemazione thread Modmail rovinato", topic=f"User ID: {user_id}"
            )
            return await self.bot.add_reaction(ctx.message, sent_emoji)

        # trovando il messaggio della genesi per ritrovare l'ID utente
        async for message in ctx.channel.history(limit=10, oldest_first=True):
            if (
                message.author == self.bot.user
                and message.embeds
                and message.embeds[0].color
                and message.embeds[0].color.value == self.bot.main_color
                and message.embeds[0].footer.text
            ):
                user_id = match_user_id(message.embeds[0].footer.text)
                if user_id != -1:
                    recipient = self.bot.get_user(user_id)
                    if recipient is None:
                        self.bot.threads.cache[user_id] = thread = Thread(
                            self.bot.threads, user_id, ctx.channel
                        )
                    else:
                        self.bot.threads.cache[user_id] = thread = Thread(
                            self.bot.threads, recipient, ctx.channel
                        )
                    thread.ready = True
                    logger.info("Impostato topic canale corrente a User ID e creato nuovo thread.")
                    await ctx.channel.edit(
                        reason="Sistemazione thread Modmail rovinato", topic=f"User ID: {user_id}"
                    )
                    return await self.bot.add_reaction(ctx.message, sent_emoji)

        else:
            logger.warning("Nessun messaggio genesi trovato.")

        # combaciando l'username dal nome del canale
        # username-1234, username-1234_1, username-1234_2
        m = re.match(r"^(.+)-(\d{4})(?:_\d+)?$", ctx.channel.name)
        if m is not None:
            users = set(
                filter(
                    lambda member: member.name == m.group(1)
                    and member.discriminator == m.group(2),
                    ctx.guild.members,
                )
            )
            if len(users) == 1:
                user = users.pop()
                name = format_channel_name(
                    user, self.bot.modmail_guild, exclude_channel=ctx.channel
                )
                recipient = self.bot.get_user(user.id)
                if user.id in self.bot.threads.cache:
                    thread = self.bot.threads.cache[user.id]
                    if thread.channel:
                        embed = discord.Embed(
                            title="Eliminazione canale",
                            description="Questo canale del thread non e' piu' in uso. "
                            f"Tutti i messaggi verranno inviati su {ctx.channel.mention} invece.",
                            color=self.bot.error_color,
                        )
                        embed.set_footer(
                            text='Per favore elimina manualmente questo canale, non usare "{prefix}close".'
                        )
                        try:
                            await thread.channel.send(embed=embed)
                        except discord.HTTPException:
                            pass
                if recipient is None:
                    self.bot.threads.cache[user.id] = thread = Thread(
                        self.bot.threads, user_id, ctx.channel
                    )
                else:
                    self.bot.threads.cache[user.id] = thread = Thread(
                        self.bot.threads, recipient, ctx.channel
                    )
                thread.ready = True
                logger.info("Impostato il topic canale a User ID e creato nuovo canale.")
                await ctx.channel.edit(
                    reason="Sistemazione thread Modmail rovinato",
                    name=name,
                    topic=f"User ID: {user.id}",
                )
                return await self.bot.add_reaction(ctx.message, sent_emoji)

            elif len(users) >= 2:
                logger.info("Multipli utenti con lo stesso nome e discriminatore.")
        return await self.bot.add_reaction(ctx.message, blocked_emoji)

    @commands.command()
    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    async def enable(self, ctx):
        """
        Riattiva le funzionalita' DM di Modmail.

        Toglie le modifiche apportate dal comando `{prefix}disable`, tutti i DM saranno consegnati dopo aver avviato questo comando.
        """
        embed = discord.Embed(
            title="Successo",
            description="Modmail ora accettera' tutti i messaggi DM.",
            color=self.bot.main_color,
        )

        if self.bot.config["dm_disabled"] != 0:
            self.bot.config["dm_disabled"] = 0
            await self.bot.config.update()

        return await ctx.send(embed=embed)

    @commands.group(invoke_without_command=True)
    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    async def disable(self, ctx):
        """
        Disattiva tutte le funzioni o solo alcune dei DM del Modmail.

        Per fermare le creazioni dei nuovi thread, fai`{prefix}disable new`.
        Per negare il DM Modmail dai thread esistenti, fai `{prefix}disable all`.
        Per vedere se la funzione DM per Modmail e' attiva, fai `{prefix}isenable`.
        """
        await ctx.send_help(ctx.command)

    @disable.command(name="new")
    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    async def disable_new(self, ctx):
        """
        Smettila di accettare nuovi thread.

        Nessun nuovo thread puo' essere creato attraverso i DM.
        """
        embed = discord.Embed(
            title="Successo",
            description="Modmail non creera' nessun nuovo thread.",
            color=self.bot.main_color,
        )
        if self.bot.config["dm_disabled"] < 1:
            self.bot.config["dm_disabled"] = 1
            await self.bot.config.update()

        return await ctx.send(embed=embed)

    @disable.command(name="all")
    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    async def disable_all(self, ctx):
        """
        Disattiva tutte le funzionalita' DM del Modmail.

        Nessun nuovo thread potra' essere creato tramite DM ne' ulteriori messaggi DM saranno consegnati.
        """
        embed = discord.Embed(
            title="Successo",
            description="Modmail non accettera' nessun messaggio DM.",
            color=self.bot.main_color,
        )

        if self.bot.config["dm_disabled"] != 2:
            self.bot.config["dm_disabled"] = 2
            await self.bot.config.update()

        return await ctx.send(embed=embed)

    @commands.command()
    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    async def isenable(self, ctx):
        """
        Controlla se le funzionalita' di DM del Modmail sono attivate.
        """

        if self.bot.config["dm_disabled"] == 1:
            embed = discord.Embed(
                title="Nuovi thread disattivati",
                description="Modmail non sta creando nuovi thread.",
                color=self.bot.error_color,
            )
        elif self.bot.config["dm_disabled"] == 2:
            embed = discord.Embed(
                title="Tutti i DM disattivati",
                description="Modmail non accetta messaggi DM per thread nuovi ed esistenti.",
                color=self.bot.error_color,
            )
        else:
            embed = discord.Embed(
                title="Attivo",
                description="Modmail accetta tutti i messaggi DM.",
                color=self.bot.main_color,
            )

        return await ctx.send(embed=embed)


def setup(bot):
    bot.add_cog(Modmail(bot))
