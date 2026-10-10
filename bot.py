import asyncio
import enum
import io
import os
import traceback
import warnings
from datetime import datetime, timezone, timedelta

import discord

# ════════════════════════════════════════════════════════════════
#  CONFIG  (variables Railway : TOKEN, GUILD_ID, LOGS_CHANNEL_ID)
# ════════════════════════════════════════════════════════════════
TOKEN     = os.environ["TOKEN"]
GUILD_ID  = int(os.environ["GUILD_ID"])
LOGS_ID   = int(os.environ["LOGS_CHANNEL_ID"])
LOG_SENT  = os.getenv("LOG_SENT_MESSAGES", "0") == "1"   # 1 = log aussi chaque message envoyé
MSG_CACHE = int(os.getenv("MSG_CACHE", "10000"))          # nb de messages gardés en mémoire

GREEN, RED, ORANGE, BLUE, GREY = 0x57F287, 0xED4245, 0xFAA61A, 0x5865F2, 0x95A5A6

# ════════════════════════════════════════════════════════════════
#  HELPERS
# ════════════════════════════════════════════════════════════════
def now():
    return datetime.now(timezone.utc)

def trunc(s, n=1000):
    s = "" if s is None else str(s)
    return s if len(s) <= n else s[: n - 1] + "…"

def ts(dt):
    t = int(dt.timestamp())
    return f"<t:{t}:F> (<t:{t}:R>)"

def who(u):
    """Mention + nom + ID, avec 🤖 si c'est un bot."""
    if u is None:
        return "Inconnu"
    if isinstance(u, discord.Object):
        return f"`{u.id}`"
    bot = " 🤖 **BOT**" if getattr(u, "bot", False) else ""
    return f"{u.mention} (`{u}` • `{u.id}`){bot}"

def link(channel_id, message_id):
    return f"https://discord.com/channels/{GUILD_ID}/{channel_id}/{message_id}"

def role_mention(r):
    return f"<@&{r.id}>"

def fmt_simple(v):
    if v is None:
        return "∅"
    if isinstance(v, bool):
        return "✅" if v else "❌"
    if isinstance(v, discord.Colour):
        return f"`{v}`"
    if isinstance(v, discord.Asset):
        return f"[lien]({v.url})"
    if isinstance(v, datetime):
        return f"<t:{int(v.timestamp())}:F>"
    if isinstance(v, timedelta):
        return f"`{int(v.total_seconds())}s`"
    if isinstance(v, enum.Enum):
        return f"`{v.name}`"
    if isinstance(v, discord.Object):
        return f"`{v.id}`"
    if isinstance(v, (list, tuple, set)):
        return ", ".join(fmt_simple(x) for x in v) or "∅"
    if hasattr(v, "mention"):
        return v.mention
    return f"`{trunc(v, 150)}`" if str(v) != "" else "∅"

def fmt_target(t):
    if t is None:
        return None
    if isinstance(t, discord.Object):
        return f"ID `{t.id}`"
    if isinstance(t, discord.Invite):
        return t.url
    mention = getattr(t, "mention", None)
    name = getattr(t, "name", None)
    tid = getattr(t, "id", None)
    parts = []
    if mention:
        parts.append(mention)
    if name and str(name) not in (mention or ""):
        parts.append(f"`{name}`")
    if tid:
        parts.append(f"(`{tid}`)")
    return trunc(" ".join(parts) or str(t), 300)

def fmt_extra(extra):
    if extra is None:
        return None
    if hasattr(extra, "mention"):
        return extra.mention
    try:
        items = [f"**{k}** : {fmt_simple(v)}" for k, v in vars(extra).items() if v is not None and not k.startswith("_")]
        if items:
            return trunc("\n".join(items), 500)
    except Exception:
        pass
    return trunc(extra, 300)

# ── Diff des permissions / overwrites ──────────────────────────
def perm_set(p):
    return {n for n, v in p if v} if p is not None else set()

def perms_diff(bv, av):
    b, a = perm_set(bv), perm_set(av)
    out = []
    if a - b:
        out.append("➕ " + ", ".join(f"`{x}`" for x in sorted(a - b)))
    if b - a:
        out.append("➖ " + ", ".join(f"`{x}`" for x in sorted(b - a)))
    return " | ".join(out)

def ow_target(t):
    if isinstance(t, discord.Object):
        tp = getattr(t, "type", None)
        return f"<@&{t.id}>" if tp is discord.Role else f"<@{t.id}>"
    return t.mention if hasattr(t, "mention") else f"`{t.id}`"

def ow_states(ow):
    return {n: v for n, v in ow if v is not None} if ow is not None else {}

SYM = {True: "✅", False: "❌", None: "➖"}

def overwrites_diff(bv, av):
    bd = {t.id: (t, o) for t, o in (bv or [])}
    ad = {t.id: (t, o) for t, o in (av or [])}
    lines = []
    for tid in list(dict.fromkeys(list(bd) + list(ad))):
        tb, ob = bd.get(tid, (None, None))
        ta, oa = ad.get(tid, (None, None))
        target = ta or tb
        sb, sa = ow_states(ob), ow_states(oa)
        diffs = [f"`{n}` {SYM[sb.get(n)]}➜{SYM[sa.get(n)]}" for n in sorted(set(sb) | set(sa)) if sb.get(n) != sa.get(n)]
        if diffs:
            lines.append(f"{ow_target(target)} : " + ", ".join(diffs))
    return "\n".join(lines)

def changes_chunks(entry):
    """Transforme entry.before / entry.after en blocs de texte lisibles."""
    try:
        b, a = dict(iter(entry.before)), dict(iter(entry.after))
    except Exception:
        return []
    lines = []
    for k in list(dict.fromkeys(list(b) + list(a))):
        bv, av = b.get(k), a.get(k)
        try:
            if k == "roles" and entry.action.name == "member_role_update":
                if bv:
                    lines.append("➖ Retirés : " + ", ".join(role_mention(r) for r in bv))
                if av:
                    lines.append("➕ Ajoutés : " + ", ".join(role_mention(r) for r in av))
            elif isinstance(bv, discord.Permissions) or isinstance(av, discord.Permissions):
                d = perms_diff(bv, av)
                if d:
                    lines.append(f"**{k}** : {d}")
            elif k == "overwrites":
                d = overwrites_diff(bv, av)
                if d:
                    lines.append(f"**permissions du salon** :\n{d}")
            else:
                lines.append(f"**{k}** : {fmt_simple(bv)} ➜ {fmt_simple(av)}")
        except Exception:
            lines.append(f"**{k}** : (illisible)")
    chunks, cur = [], ""
    for line in lines:
        line = trunc(line, 900)
        if len(cur) + len(line) + 1 > 1000:
            chunks.append(cur)
            cur = ""
        cur += line + "\n"
    if cur:
        chunks.append(cur)
    return chunks[:4]

# ════════════════════════════════════════════════════════════════
#  ENVOI DES LOGS (file d'attente → anti rate-limit)
# ════════════════════════════════════════════════════════════════
queue: asyncio.Queue = asyncio.Queue()

def emit(embed, file=None):
    if embed.timestamp is None:
        embed.timestamp = now()
    queue.put_nowait((embed, file))

async def sender():
    channel, pending = None, None
    while True:
        embed, file = pending or await queue.get()
        pending = None
        embeds, size = [embed], len(embed)
        if file is None:
            while len(embeds) < 10:
                try:
                    nxt = queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
                if nxt[1] is not None or size + len(nxt[0]) > 5500:
                    pending = nxt
                    break
                embeds.append(nxt[0])
                size += len(nxt[0])
        for _ in range(3):
            try:
                if channel is None:
                    channel = client.get_channel(LOGS_ID) or await client.fetch_channel(LOGS_ID)
                await channel.send(embeds=embeds, file=file, allowed_mentions=discord.AllowedMentions.none())
                break
            except Exception:
                traceback.print_exc()
                if file is not None:
                    file = None  # ne retente pas le fichier
                await asyncio.sleep(3)
        await asyncio.sleep(0.4)

# ════════════════════════════════════════════════════════════════
#  CLIENT
# ════════════════════════════════════════════════════════════════
class LogClient(discord.Client):
    async def setup_hook(self):
        asyncio.create_task(sender())

client = LogClient(
    intents=discord.Intents.all(),
    max_messages=MSG_CACHE,
    status=discord.Status.online,
    activity=discord.Activity(type=discord.ActivityType.watching, name="Shotojo \U0001F338 #\U0001F1EF\U0001F1F5 • Logs"),
)

_started = False

@client.event
async def on_ready():
    global _started
    print(f"✅ {client.user} connecté (ID: {client.user.id})")
    if _started:
        return
    _started = True
    guild = client.get_guild(GUILD_ID)
    e = discord.Embed(title="✅ Bot de logs en ligne", color=GREEN)
    if guild:
        e.description = f"Surveillance du serveur **{guild.name}** démarrée."
        if not guild.me.guild_permissions.view_audit_log:
            e.add_field(name="⚠️ Permission manquante",
                        value="Il me faut **Voir les logs du serveur** pour savoir *qui* fait les actions.", inline=False)
    else:
        e.color = RED
        e.description = "⚠️ Le bot n'est pas dans le serveur configuré (GUILD_ID)."
    emit(e)

# ════════════════════════════════════════════════════════════════
#  MESSAGES : suppression
# ════════════════════════════════════════════════════════════════
async def find_deleter(guild, author_id, channel_id):
    try:
        async for en in guild.audit_logs(limit=10, action=discord.AuditLogAction.message_delete):
            if (now() - en.created_at).total_seconds() > 15:
                break
            ch = getattr(en.extra, "channel", None)
            if en.target is not None and en.target.id == author_id and getattr(ch, "id", None) == channel_id:
                return en.user
    except discord.Forbidden:
        return "forbidden"
    except Exception:
        pass
    return None

def deleter_text(d):
    if d == "forbidden":
        return "❓ Permission *Voir les logs du serveur* manquante"
    if d:
        return who(d)
    return "👤 Lui-même (ou suppression non tracée par Discord)"

def msg_details(e, msg):
    if msg.attachments:
        e.add_field(name="📎 Pièces jointes",
                    value=trunc("\n".join(f"[{a.filename}]({a.url})" for a in msg.attachments), 1000), inline=False)
    if msg.stickers:
        e.add_field(name="🏷️ Stickers", value=", ".join(s.name for s in msg.stickers), inline=False)
    if msg.embeds:
        e.add_field(name="🧩 Embeds", value=f"{len(msg.embeds)} embed(s)", inline=True)
    if msg.reference and msg.reference.message_id:
        e.add_field(name="↩️ En réponse à", value=f"[message]({link(msg.reference.channel_id, msg.reference.message_id)})", inline=True)

async def handle_delete(payload):
    try:
        msg = payload.cached_message
        guild = client.get_guild(GUILD_ID)
        e = discord.Embed(title="🗑️ Message supprimé", color=RED)
        e.add_field(name="📍 Salon", value=f"<#{payload.channel_id}>", inline=True)
        if msg:
            await asyncio.sleep(1.5)
            deleter = await find_deleter(guild, msg.author.id, payload.channel_id) if guild else None
            e.add_field(name="✍️ Auteur", value=who(msg.author), inline=True)
            e.add_field(name="🗑️ Supprimé par", value=deleter_text(deleter), inline=False)
            e.add_field(name="📅 Envoyé", value=ts(msg.created_at), inline=True)
            e.add_field(name="🆔 Message", value=f"`{msg.id}`", inline=True)
            e.add_field(name="💬 Contenu", value=trunc(msg.content, 1000) or "*(aucun texte)*", inline=False)
            msg_details(e, msg)
            e.set_thumbnail(url=msg.author.display_avatar.url)
        else:
            e.add_field(name="🆔 Message", value=f"`{payload.message_id}`", inline=True)
            e.add_field(name="ℹ️", value="Contenu et auteur inconnus (message envoyé avant le démarrage du bot, donc pas en cache).", inline=False)
        emit(e)
    except Exception:
        traceback.print_exc()

@client.event
async def on_raw_message_delete(payload):
    if payload.guild_id != GUILD_ID or payload.channel_id == LOGS_ID:
        return
    asyncio.create_task(handle_delete(payload))

async def handle_bulk(payload):
    try:
        guild = client.get_guild(GUILD_ID)
        await asyncio.sleep(1.5)
        deleter = None
        try:
            async for en in guild.audit_logs(limit=5, action=discord.AuditLogAction.message_bulk_delete):
                if (now() - en.created_at).total_seconds() > 15:
                    break
                if en.target is not None and en.target.id == payload.channel_id:
                    deleter = en.user
                    break
        except discord.Forbidden:
            deleter = "forbidden"
        except Exception:
            pass
        e = discord.Embed(title="🧹 Suppression en masse", color=RED)
        e.add_field(name="📍 Salon", value=f"<#{payload.channel_id}>", inline=True)
        e.add_field(name="🔢 Messages", value=str(len(payload.message_ids)), inline=True)
        e.add_field(name="🗑️ Supprimé par", value=deleter_text(deleter) if deleter else "❓ Inconnu", inline=False)
        cached = sorted(payload.cached_messages, key=lambda m: m.created_at)
        file = None
        if cached:
            e.add_field(name="💾 En cache", value=f"{len(cached)} message(s) récupérés (voir fichier joint)", inline=False)
            text = "\n".join(
                f"[{m.created_at:%Y-%m-%d %H:%M:%S}] {m.author} ({m.author.id}): {m.content}"
                + (" | PJ: " + ", ".join(a.url for a in m.attachments) if m.attachments else "")
                for m in cached
            )
            file = discord.File(io.BytesIO(text.encode("utf-8")), filename="messages_supprimes.txt")
        emit(e, file)
    except Exception:
        traceback.print_exc()

@client.event
async def on_raw_bulk_message_delete(payload):
    if payload.guild_id != GUILD_ID or payload.channel_id == LOGS_ID:
        return
    asyncio.create_task(handle_bulk(payload))

# ════════════════════════════════════════════════════════════════
#  MESSAGES : modification / envoi / réactions
# ════════════════════════════════════════════════════════════════
@client.event
async def on_raw_message_edit(payload):
    try:
        if payload.guild_id != GUILD_ID or payload.channel_id == LOGS_ID:
            return
        if "content" not in payload.data:
            return
        after = payload.data["content"]
        old = payload.cached_message
        if old is not None and old.content == after:
            return
        e = discord.Embed(title="✏️ Message modifié", color=ORANGE)
        author = old.author if old else None
        if author is None and payload.data.get("author"):
            uid = int(payload.data["author"]["id"])
            author = client.get_user(uid) or discord.Object(id=uid)
        e.add_field(name="✍️ Auteur", value=who(author) if author else "Inconnu", inline=True)
        e.add_field(name="📍 Salon", value=f"<#{payload.channel_id}>", inline=True)
        e.add_field(name="🔗 Lien", value=f"[Aller au message]({link(payload.channel_id, payload.message_id)})", inline=True)
        e.add_field(name="⬅️ Avant", value=trunc(old.content, 1000) or "*(vide)*" if old else "*(inconnu — pas en cache)*", inline=False)
        e.add_field(name="➡️ Après", value=trunc(after, 1000) or "*(vide)*", inline=False)
        if isinstance(author, (discord.User, discord.Member)):
            e.set_thumbnail(url=author.display_avatar.url)
        emit(e)
    except Exception:
        traceback.print_exc()

@client.event
async def on_message(message):
    try:
        if not LOG_SENT or message.guild is None or message.guild.id != GUILD_ID or message.channel.id == LOGS_ID:
            return
        e = discord.Embed(title="💬 Message envoyé", color=BLUE if not message.author.bot else GREY)
        e.add_field(name="✍️ Auteur", value=who(message.author), inline=True)
        e.add_field(name="📍 Salon", value=message.channel.mention, inline=True)
        e.add_field(name="🔗 Lien", value=f"[Aller au message]({message.jump_url})", inline=True)
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                inter = message.interaction
            if inter is not None:
                e.add_field(name="⌨️ Commande slash", value=f"`/{inter.name}` par {inter.user.mention}", inline=False)
        except Exception:
            pass
        e.add_field(name="💬 Contenu", value=trunc(message.content, 1000) or "*(aucun texte)*", inline=False)
        msg_details(e, message)
        e.set_thumbnail(url=message.author.display_avatar.url)
        emit(e)
    except Exception:
        traceback.print_exc()

@client.event
async def on_raw_reaction_add(payload):
    if payload.guild_id != GUILD_ID or payload.channel_id == LOGS_ID:
        return
    bot = " 🤖" if payload.member and payload.member.bot else ""
    e = discord.Embed(title="➕ Réaction ajoutée", color=GREEN,
                      description=f"<@{payload.user_id}>{bot} (`{payload.user_id}`) a réagi {payload.emoji} sur [ce message]({link(payload.channel_id, payload.message_id)}) dans <#{payload.channel_id}>")
    emit(e)

@client.event
async def on_raw_reaction_remove(payload):
    if payload.guild_id != GUILD_ID or payload.channel_id == LOGS_ID:
        return
    e = discord.Embed(title="➖ Réaction retirée", color=GREY,
                      description=f"<@{payload.user_id}> (`{payload.user_id}`) a retiré {payload.emoji} sur [ce message]({link(payload.channel_id, payload.message_id)}) dans <#{payload.channel_id}>")
    emit(e)

@client.event
async def on_raw_reaction_clear(payload):
    if payload.guild_id != GUILD_ID or payload.channel_id == LOGS_ID:
        return
    emit(discord.Embed(title="🧽 Toutes les réactions retirées", color=ORANGE,
                       description=f"Sur [ce message]({link(payload.channel_id, payload.message_id)}) dans <#{payload.channel_id}>"))

# ════════════════════════════════════════════════════════════════
#  MEMBRES
# ════════════════════════════════════════════════════════════════
@client.event
async def on_member_join(member):
    if member.guild.id != GUILD_ID:
        return
    e = discord.Embed(title="🤖 Bot arrivé" if member.bot else "📥 Membre arrivé", color=GREEN)
    e.add_field(name="👤 Membre", value=who(member), inline=False)
    e.add_field(name="📅 Compte créé", value=ts(member.created_at), inline=False)
    if (now() - member.created_at).days < 7:
        e.add_field(name="⚠️ Compte récent", value=f"Créé il y a {(now() - member.created_at).days} jour(s)", inline=False)
    e.add_field(name="👥 Membres", value=str(member.guild.member_count), inline=True)
    e.set_thumbnail(url=member.display_avatar.url)
    emit(e)

@client.event
async def on_member_remove(member):
    if member.guild.id != GUILD_ID:
        return
    e = discord.Embed(title="📤 Membre parti", color=RED)
    e.add_field(name="👤 Membre", value=who(member), inline=False)
    if member.joined_at:
        e.add_field(name="📅 Avait rejoint", value=ts(member.joined_at), inline=False)
    roles = [role_mention(r) for r in member.roles if r.id != member.guild.id]
    if roles:
        e.add_field(name="🎭 Rôles", value=trunc(", ".join(roles), 1000), inline=False)
    e.add_field(name="ℹ️", value="Départ volontaire, expulsion ou ban : voir l'entrée d'audit juste à côté.", inline=False)
    e.add_field(name="👥 Membres", value=str(member.guild.member_count), inline=True)
    e.set_thumbnail(url=member.display_avatar.url)
    emit(e)

@client.event
async def on_user_update(before, after):
    guild = client.get_guild(GUILD_ID)
    if not guild or not guild.get_member(after.id):
        return
    lines = []
    if before.name != after.name:
        lines.append(f"**Pseudo** : `{before.name}` ➜ `{after.name}`")
    if before.global_name != after.global_name:
        lines.append(f"**Nom d'affichage** : `{before.global_name}` ➜ `{after.global_name}`")
    if before.avatar != after.avatar:
        lines.append(f"**Avatar** : [avant]({before.display_avatar.url}) ➜ [après]({after.display_avatar.url})")
    if lines:
        e = discord.Embed(title="🪪 Profil modifié", color=ORANGE, description=f"{who(after)}\n" + "\n".join(lines))
        e.set_thumbnail(url=after.display_avatar.url)
        emit(e)

@client.event
async def on_member_update(before, after):
    # Pseudos / rôles / timeouts : gérés via les logs d'audit (avec l'auteur). Ici, ce que l'audit ne donne pas.
    if after.guild.id != GUILD_ID:
        return
    if before.guild_avatar != after.guild_avatar:
        new = after.guild_avatar.url if after.guild_avatar else None
        old = before.guild_avatar.url if before.guild_avatar else None
        e = discord.Embed(title="🖼️ Avatar de serveur modifié", color=ORANGE,
                          description=f"{who(after)}\nAvant : {old or '∅'}\nAprès : {new or '∅'}")
        emit(e)
    if before.premium_since != after.premium_since:
        boost = after.premium_since is not None
        emit(discord.Embed(title="💎 Boost " + ("ajouté" if boost else "retiré"), color=BLUE if boost else GREY,
                           description=who(after)))

# ════════════════════════════════════════════════════════════════
#  VOCAL
# ════════════════════════════════════════════════════════════════
VOICE_FLAGS = [
    ("self_mute", "🎙️ Micro coupé (par lui)"), ("self_deaf", "🎧 Casque coupé (par lui)"),
    ("mute", "🔇 Mute serveur"), ("deaf", "🔕 Sourd serveur"),
    ("self_stream", "📺 Partage d'écran"), ("self_video", "📷 Caméra"), ("suppress", "🚫 Supprimé (stage)"),
]

@client.event
async def on_voice_state_update(member, before, after):
    if member.guild.id != GUILD_ID:
        return
    if before.channel != after.channel:
        if before.channel is None:
            e = discord.Embed(title="🔊 Connexion vocal", color=GREEN, description=f"{who(member)}\nA rejoint {after.channel.mention}")
        elif after.channel is None:
            e = discord.Embed(title="🔇 Déconnexion vocal", color=RED, description=f"{who(member)}\nA quitté {before.channel.mention}")
        else:
            e = discord.Embed(title="↔️ Changement de vocal", color=ORANGE,
                              description=f"{who(member)}\n{before.channel.mention} ➜ {after.channel.mention}")
        emit(e)
    lines = []
    for attr, label in VOICE_FLAGS:
        if getattr(before, attr) != getattr(after, attr):
            lines.append(f"{label} : {'✅ activé' if getattr(after, attr) else '❌ désactivé'}")
    if lines:
        chan = after.channel or before.channel
        emit(discord.Embed(title="🎛️ État vocal modifié", color=BLUE,
                           description=f"{who(member)}" + (f" dans {chan.mention}" if chan else "") + "\n" + "\n".join(lines)))

# ════════════════════════════════════════════════════════════════
#  AUTOMOD
# ════════════════════════════════════════════════════════════════
@client.event
async def on_automod_action(execution):
    try:
        if execution.guild_id != GUILD_ID:
            return
        e = discord.Embed(title="🛑 AutoMod déclenché", color=RED)
        e.add_field(name="👤 Membre", value=f"<@{execution.user_id}> (`{execution.user_id}`)", inline=True)
        if execution.channel_id:
            e.add_field(name="📍 Salon", value=f"<#{execution.channel_id}>", inline=True)
        e.add_field(name="⚙️ Action", value=f"`{execution.action.type.name}`", inline=True)
        rule = f"`{execution.rule_id}`"
        e.add_field(name="📏 Règle", value=rule, inline=True)
        if execution.matched_keyword:
            e.add_field(name="🔑 Mot détecté", value=f"`{trunc(execution.matched_keyword, 100)}`", inline=True)
        if execution.content:
            e.add_field(name="💬 Contenu", value=trunc(execution.content, 1000), inline=False)
        emit(e)
    except Exception:
        traceback.print_exc()

# ════════════════════════════════════════════════════════════════
#  LOGS D'AUDIT : toutes les actions (humains ET bots) avec l'auteur
# ════════════════════════════════════════════════════════════════
LABELS = {
    "guild_update": ("⚙️", "Serveur modifié"),
    "channel_create": ("📁", "Salon créé"), "channel_update": ("📝", "Salon modifié"), "channel_delete": ("🗑️", "Salon supprimé"),
    "overwrite_create": ("🔐", "Permission de salon ajoutée"), "overwrite_update": ("🔐", "Permission de salon modifiée"),
    "overwrite_delete": ("🔐", "Permission de salon retirée"),
    "kick": ("👢", "Membre expulsé"), "member_prune": ("🧹", "Purge de membres inactifs"),
    "ban": ("🔨", "Membre banni"), "unban": ("✅", "Membre débanni"),
    "member_update": ("✏️", "Membre modifié (pseudo / timeout / mute…)"),
    "member_role_update": ("🎭", "Rôles d'un membre modifiés"),
    "member_move": ("↔️", "Membre déplacé en vocal"), "member_disconnect": ("🔌", "Membre déconnecté du vocal"),
    "bot_add": ("🤖", "Bot ajouté au serveur"),
    "role_create": ("🎭", "Rôle créé"), "role_update": ("🎭", "Rôle modifié"), "role_delete": ("🎭", "Rôle supprimé"),
    "invite_create": ("🔗", "Invitation créée"), "invite_update": ("🔗", "Invitation modifiée"), "invite_delete": ("🔗", "Invitation supprimée"),
    "webhook_create": ("🪝", "Webhook créé"), "webhook_update": ("🪝", "Webhook modifié"), "webhook_delete": ("🪝", "Webhook supprimé"),
    "emoji_create": ("😀", "Emoji créé"), "emoji_update": ("😀", "Emoji modifié"), "emoji_delete": ("😀", "Emoji supprimé"),
    "message_pin": ("📌", "Message épinglé"), "message_unpin": ("📌", "Message désépinglé"),
    "integration_create": ("🧩", "Intégration ajoutée"), "integration_update": ("🧩", "Intégration modifiée"), "integration_delete": ("🧩", "Intégration supprimée"),
    "stage_instance_create": ("🎤", "Stage créé"), "stage_instance_update": ("🎤", "Stage modifié"), "stage_instance_delete": ("🎤", "Stage supprimé"),
    "sticker_create": ("🏷️", "Sticker créé"), "sticker_update": ("🏷️", "Sticker modifié"), "sticker_delete": ("🏷️", "Sticker supprimé"),
    "scheduled_event_create": ("📆", "Évènement créé"), "scheduled_event_update": ("📆", "Évènement modifié"), "scheduled_event_delete": ("📆", "Évènement supprimé"),
    "thread_create": ("🧵", "Fil créé"), "thread_update": ("🧵", "Fil modifié"), "thread_delete": ("🧵", "Fil supprimé"),
    "app_command_permission_update": ("⌨️", "Permissions de commande modifiées"),
    "automod_rule_create": ("🛑", "Règle AutoMod créée"), "automod_rule_update": ("🛑", "Règle AutoMod modifiée"), "automod_rule_delete": ("🛑", "Règle AutoMod supprimée"),
    "automod_block_message": ("🛑", "AutoMod : message bloqué"), "automod_flag_message": ("🛑", "AutoMod : message signalé"),
    "automod_timeout_member": ("🛑", "AutoMod : membre mis en timeout"),
}
RED_ACTIONS = {"kick", "ban", "member_prune", "member_disconnect", "automod_block_message", "automod_timeout_member"}
GREEN_ACTIONS = {"unban", "bot_add"}
SKIP_AUDIT = {discord.AuditLogAction.message_delete, discord.AuditLogAction.message_bulk_delete}  # gérés par les events messages

def audit_color(entry):
    n = entry.action.name
    if n in RED_ACTIONS:
        return RED
    if n in GREEN_ACTIONS:
        return GREEN
    cat = entry.category
    if cat == discord.AuditLogActionCategory.create:
        return GREEN
    if cat == discord.AuditLogActionCategory.delete:
        return RED
    if cat == discord.AuditLogActionCategory.update:
        return ORANGE
    return GREY

@client.event
async def on_audit_log_entry_create(entry):
    try:
        if entry.guild.id != GUILD_ID or entry.action in SKIP_AUDIT:
            return
        name = entry.action.name
        emoji, label = LABELS.get(name, ("📋", name.replace("_", " ").capitalize()))
        e = discord.Embed(title=f"{emoji} {label}", color=audit_color(entry), timestamp=entry.created_at)
        e.add_field(name="👤 Exécutant", value=who(entry.user) if entry.user else f"`{entry.user_id}`", inline=True)
        target = fmt_target(entry.target)
        if target:
            e.add_field(name="🎯 Cible", value=target, inline=True)
        if entry.reason:
            e.add_field(name="📝 Raison", value=trunc(entry.reason, 500), inline=False)
        extra = fmt_extra(entry.extra)
        if extra:
            e.add_field(name="ℹ️ Détails", value=extra, inline=False)
        for i, chunk in enumerate(changes_chunks(entry)):
            e.add_field(name="🔄 Changements" if i == 0 else "🔄 (suite)", value=chunk, inline=False)
        e.set_footer(text=f"Audit ID {entry.id}")
        emit(e)
    except Exception:
        traceback.print_exc()

# ════════════════════════════════════════════════════════════════
#  LANCEMENT
# ════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    client.run(TOKEN)
