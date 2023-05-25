import asyncio
import logging
import sys
from datetime import datetime
from typing import Optional, Union

import discord
import pytz
from aiocache import cached
from redbot.core import version_info
from redbot.core.utils.chat_formatting import humanize_list

from .abc import MixinMeta
from .common.utils import get_chat, get_embedding, num_tokens_from_string
from .models import Conversation, GuildSettings

log = logging.getLogger("red.vrt.assistant.api")


class API(MixinMeta):
    @cached(ttl=30)
    async def chat_async(
        self,
        message: str,
        author: Union[discord.Member, int],
        guild: discord.Guild,
        channel: Union[discord.TextChannel, discord.Thread, discord.ForumChannel, int],
        conf: GuildSettings,
    ):
        """Call the API asynchronously"""
        conversation = self.chats.get_conversation(
            author if isinstance(author, int) else author.id,
            channel if isinstance(channel, int) else channel.id,
            guild.id,
        )
        if isinstance(author, int):
            author = guild.get_member(author)
        if isinstance(channel, int):
            channel = guild.get_channel(channel)
        try:
            reply = await asyncio.to_thread(
                self.prepare_call, message, guild, conf, conversation, author, channel
            )
        finally:
            conversation.cleanup(conf)
        return reply

    def prepare_call(
        self,
        message: str,
        guild: discord.Guild,
        conf: GuildSettings,
        conversation: Conversation,
        author: Optional[discord.Member],
        channel: Optional[Union[discord.TextChannel, discord.Thread, discord.ForumChannel]],
    ) -> str:
        """Prepare content for calling the GPT API

        Args:
            message (str): question or chat message
            guild (discord.Guild): guild associated with the chat
            conf (GuildSettings): config data
            conversation (Conversation): user's conversation object for chat history
            author (Optional[discord.Member]): user chatting with the bot
            channel (Optional[Union[discord.TextChannel, discord.Thread, discord.ForumChannel]]): channel for context

        Returns:
            str: the response from ChatGPT
        """
        now = datetime.now().astimezone(pytz.timezone(conf.timezone))

        query_embedding = get_embedding(text=message, api_key=conf.api_key)
        if not query_embedding:
            log.info(f"Could not get embedding for message: {message}")

        params = {
            "botname": self.bot.user.name,
            "timestamp": f"<t:{round(now.timestamp())}:F>",
            "day": now.strftime("%A"),
            "date": now.strftime("%B %d, %Y"),
            "time": now.strftime("%I:%M %p"),
            "timetz": now.strftime("%I:%M %p %Z"),
            "members": guild.member_count,
            "username": author.name if author else "",
            "user": author.display_name if author else "",
            "datetime": str(datetime.now()),
            "roles": humanize_list([role.name for role in author.roles]) if author else "",
            "avatar": author.display_avatar.url if author else "",
            "owner": guild.owner,
            "servercreated": f"<t:{round(guild.created_at.timestamp())}:F>",
            "server": guild.name,
            "messages": len(conversation.messages),
            "tokens": conversation.user_token_count(message=message),
            "retention": conf.max_retention,
            "retentiontime": conf.max_retention_time,
            "py": f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
            "dpy": discord.__version__,
            "red": version_info,
            "cogs": humanize_list([self.bot.get_cog(cog).qualified_name for cog in self.bot.cogs]),
            "channelname": channel.name if channel else "",
            "channelmention": channel.mention if channel else "",
            "topic": channel.topic if channel and isinstance(channel, discord.TextChannel) else "",
        }
        system_prompt = conf.system_prompt.format(**params)
        initial_prompt = conf.prompt.format(**params)

        # Dynamically clean up the conversation to prevent going over token limit
        max_usage = round(conf.max_tokens * 0.9)
        prompt_tokens = num_tokens_from_string(system_prompt + initial_prompt)
        while (conversation.token_count() + prompt_tokens) > max_usage:
            conversation.messages.pop(0)

        total_tokens = conversation.token_count() + prompt_tokens + num_tokens_from_string(message)

        embeddings = []
        for i in conf.get_related_embeddings(query_embedding):
            if num_tokens_from_string(f"\nContext:\n{i[1]}\n\n") + total_tokens < max_usage:
                embeddings.append(f"{i[1]}")

        if embeddings:
            joined = "\n".join(embeddings)
            if conf.embed_method == "static":
                message = f"Context:\n{joined}\n\nChat: {message}"
            elif conf.embed_method == "dynamic":
                initial_prompt += f"\nContext:\n{joined}"
            elif conf.embed_method == "hybrid" and len(embeddings) > 1:
                initial_prompt += f"\nContext:\n{embeddings[0]}"
                joined = "\n".join(embeddings[1:])
                message = f"Context:\n{joined}\n\nChat: {message}"
            else:
                initial_prompt += f"\nContext:\n{embeddings[0]}"

        conversation.update_messages(message, "user")
        messages = conversation.prepare_chat(system_prompt, initial_prompt)
        reply = get_chat(model=conf.model, messages=messages, temperature=0, api_key=conf.api_key)
        conversation.update_messages(reply, "assistant")
        return reply
