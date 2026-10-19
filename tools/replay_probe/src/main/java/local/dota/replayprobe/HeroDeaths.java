package local.dota.replayprobe;

import skadistats.clarity.event.Insert;
import skadistats.clarity.io.Util;
import skadistats.clarity.model.CombatLogEntry;
import skadistats.clarity.model.Entity;
import skadistats.clarity.processor.entities.Entities;
import skadistats.clarity.processor.gameevents.OnCombatLogEntry;
import skadistats.clarity.processor.runner.Context;
import skadistats.clarity.wire.dota.common.proto.DOTACombatLog.DOTA_COMBATLOG_TYPES;
import skadistats.clarity.wire.shared.demo.proto.Demo;

import java.util.ArrayList;
import java.util.HashSet;
import java.util.Iterator;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;

/** Death-only listener, registered exclusively by --hero-deaths. */
final class HeroDeaths {
    record Primary(int slot, int team, int heroId, String unitName, int handle,
                   String entityClass, boolean verified, boolean cloneAmbiguous, Integer deaths) {}
    record Death(String target, Integer targetTeam, boolean targetHero, boolean illusion,
                 boolean illusionFlagPresent, Float timestamp, Float timestampRaw,
                 String attacker, Integer attackerTeam, boolean attackerHero, boolean attackerIllusion,
                 String damageSource, String inflictor, String targetSource, Boolean reincarnate) {}

    @Insert private Entities entities;
    private record FooterHero(String name, int team) {}
    private final Map<Long, FooterHero> footerByPlayer = new LinkedHashMap<>();
    private final List<Map<String, Object>> events = new ArrayList<>();
    private final List<Map<String, Object>> rejected = new ArrayList<>();
    private final Map<Integer, List<Map<String, Object>>> waiting = new LinkedHashMap<>();
    private final Set<String> seen = new HashSet<>();
    private Map<String, Object> lastFrame;
    private int lastFrameTick = -1;
    private int nonHeroDeaths;
    private int illusionDeaths;
    private int duplicates;

    void footer(List<Demo.CGameInfo.CDotaGameInfo.CPlayerInfo> players) {
        for (var player : players) {
            if (player.hasSteamid() && player.hasHeroName() && player.hasGameTeam()) {
                if (footerByPlayer.put(player.getSteamid(), new FooterHero(player.getHeroName(), player.getGameTeam())) != null) {
                    throw new IllegalStateException("Ambiguous footer player identity");
                }
            }
        }
    }

    @OnCombatLogEntry
    public void onCombatLogEntry(Context context, CombatLogEntry entry) {
        if (!entry.hasType() || entry.getType() != DOTA_COMBATLOG_TYPES.DOTA_COMBATLOG_DEATH) return;
        Death death = new Death(
                entry.hasTargetName() ? entry.getTargetName() : null,
                entry.hasTargetTeam() ? entry.getTargetTeam() : null,
                entry.isTargetHero(), entry.isTargetIllusion(), entry.hasTargetIllusion(),
                entry.hasTimestamp() ? entry.getTimestamp() : null,
                entry.hasTimestampRaw() ? entry.getTimestampRaw() : null,
                entry.hasAttackerName() ? entry.getAttackerName() : null,
                entry.hasAttackerTeam() ? entry.getAttackerTeam() : null,
                entry.isAttackerHero(), entry.isAttackerIllusion(),
                entry.hasDamageSourceName() ? entry.getDamageSourceName() : null,
                entry.hasInflictorName() ? entry.getInflictorName() : null,
                entry.hasTargetSourceName() ? entry.getTargetSourceName() : null,
                entry.hasWillReincarnate() ? entry.isWillReincarnate() : null
        );
        accept(context.getTick(), death, death.targetHero && !death.illusion ? roster() : List.of());
    }

    static Primary identify(String name, Integer team, List<Primary> roster) {
        List<Primary> matches = roster.stream().filter(p -> name != null && name.equals(p.unitName)
                && (team == null || team == p.team)).toList();
        return matches.size() == 1 && matches.get(0).verified && !matches.get(0).cloneAmbiguous
                ? matches.get(0) : null;
    }

    void accept(int tick, Death death, List<Primary> roster) {
        if (!death.targetHero) { nonHeroDeaths++; return; }
        if (death.illusion) { illusionDeaths++; return; }
        Primary victim = identify(death.target, death.targetTeam, roster);
        if (victim == null) {
            rejected.add(ReplayProbe.mapOf("replay_tick", tick, "target_unit_name", death.target,
                    "target_team", death.targetTeam, "reason", "PRIMARY_NAME_TEAM_OR_CLONE_IDENTITY_UNRESOLVED"));
            return;
        }
        String key = victim.slot + ":" + death.timestamp + ":" + death.timestampRaw + ":" + death.attacker;
        if (death.timestamp == null && death.timestampRaw == null) key += ":" + tick;
        if (!seen.add(key)) { duplicates++; return; }
        Primary killer = death.attackerHero && !death.attackerIllusion
                ? identify(death.attacker, death.attackerTeam, roster) : null;
        Map<String, Object> event = ReplayProbe.mapOf(
                "replay_tick", tick, "combat_log_timestamp", death.timestamp,
                "combat_log_timestamp_raw", death.timestampRaw,
                "timestamp_semantics_status", "UNVERIFIED_NATIVE_FIELDS",
                "victim_unit_name", death.target, "victim_team", victim.team == 2 ? "RADIANT" : "DIRE",
                "player_slot", victim.slot, "hero_id", victim.heroId,
                "selected_primary_entity_handle", victim.handle,
                "selected_primary_entity_class", victim.entityClass,
                "identity_status", "UNIQUE_FOOTER_PLAYER_TEAM_SELECTED_PRIMARY_NONCLONE",
                "combat_log_target_team", death.targetTeam,
                "combat_log_target_hero", death.targetHero,
                "combat_log_target_illusion", death.illusion,
                "target_illusion_flag_present", death.illusionFlagPresent,
                "target_source_unit_name", death.targetSource,
                "will_reincarnate", death.reincarnate,
                "attacker_unit_name", death.attacker, "attacker_team", death.attackerTeam,
                "attacker_is_hero", death.attackerHero, "attacker_is_illusion", death.attackerIllusion,
                "killer_player_slot", killer == null ? null : killer.slot,
                "killer_identity_status", killer == null ? "UNRESOLVED_OR_NONPRIMARY" : "UNIQUE_PRIMARY_NAME_TEAM",
                "damage_source_unit_name", death.damageSource, "inflictor_name", death.inflictor
        );
        events.add(event);
        if (lastFrameTick == tick) attachClock(event, lastFrame);
        else waiting.computeIfAbsent(tick, ignored -> new ArrayList<>()).add(event);
    }

    void frame(int tick, Map<String, Object> frame) {
        lastFrameTick = tick;
        lastFrame = new LinkedHashMap<>(frame);
        List<Map<String, Object>> pending = waiting.remove(tick);
        if (pending != null) pending.forEach(event -> attachClock(event, lastFrame));
    }

    static void attachClock(Map<String, Object> event, Map<String, Object> frame) {
        event.putAll(frame);
        event.put("event_game_time_seconds", null);
        event.put("event_minus_frame_seconds", null);
        Object nativeTime = event.get("combat_log_timestamp");
        Object anchor = frame.get("frame_game_start_time_property");
        Object normalized = frame.get("frame_game_time_seconds");
        event.put("timestamp_raw_equals_timestamp", nativeTime != null
                && nativeTime.equals(event.get("combat_log_timestamp_raw")));
        if (!(nativeTime instanceof Number ts) || !(anchor instanceof Number zero)
                || !(normalized instanceof Number observed) || !Boolean.TRUE.equals(frame.get("frame_clock_proven"))) {
            event.put("timestamp_semantics_status", "MISSING_TIMESTAMP_OR_PROVEN_CLOCK");
            return;
        }
        float source = ts.floatValue(), start = zero.floatValue(), actual = observed.floatValue();
        float candidate = source - start;
        double error = (double) candidate - actual;
        double tolerance = ((Number) frame.get("frame_millis_per_tick")).doubleValue() / 1000
                + 2 * Math.max(Math.ulp(source), Math.ulp(actual));
        if (!Float.isFinite(candidate) || !Float.isFinite(actual) || Math.abs(error) > tolerance
                || Boolean.TRUE.equals(frame.get("frame_synthetic"))
                || Boolean.TRUE.equals(frame.get("frame_game_end_frozen"))) {
            event.put("timestamp_semantics_status", "TIME_DOMAIN_OR_DELIVERY_FRAME_UNVERIFIED");
            return;
        }
        // Independently witnessed GameRules anchor, not a fitted timestamp offset.
        // Never clamp native event time to delivery time, or subtract paused ticks twice.
        event.put("event_game_time_seconds", candidate);
        event.put("event_minus_frame_seconds", error);
        event.put("timestamp_semantics_status", "ENGINE_TIMESTAMP_MINUS_GAME_START_CORROBORATED_AT_DELIVERY");
    }

    List<Primary> roster() {
        Entity resource = entities.getByDtName("CDOTA_PlayerResource");
        if (resource == null) return List.of();
        List<Primary> result = new ArrayList<>();
        for (int slot = 0; slot < 64; slot++) {
            String index = Util.arrayIdxToString(slot);
            Integer team = integer(resource, "m_vecPlayerData." + index + ".m_iPlayerTeam");
            if (team == null || (team != 2 && team != 3)) continue;
            Integer handle = integer(resource, "m_vecPlayerTeamData." + index + ".m_hSelectedHero");
            Integer heroId = integer(resource, "m_vecPlayerTeamData." + index + ".m_nSelectedHeroID");
            Entity hero = handle == null ? null : entities.getByHandle(handle);
            Object playerId = property(resource, "m_vecPlayerData." + index + ".m_iPlayerSteamID");
            FooterHero footer = playerId instanceof Number n ? footerByPlayer.get(n.longValue()) : null;
            String name = footer != null && footer.team == team ? footer.name : null;
            boolean verified = HeroPositions.primaryIdentityMatches(team, heroId, handle, HeroPositions.sourceOf(hero))
                    && Boolean.FALSE.equals(property(hero, "m_bIsClone")) && name != null;
            boolean ambiguous = false;
            if (hero != null) {
                Iterator<Entity> copies = entities.getAllByDtName(hero.getDtClass().getDtName());
                while (copies.hasNext()) {
                    Entity copy = copies.next();
                    if (copy.isExistent() && copy.isActive() && Boolean.TRUE.equals(property(copy, "m_bIsClone"))) ambiguous = true;
                }
            }
            result.add(new Primary(slot, team, heroId == null ? 0 : heroId, name, handle == null ? -1 : handle,
                    hero == null ? null : hero.getDtClass().getDtName(), verified, ambiguous,
                    integer(resource, "m_vecPlayerTeamData." + index + ".m_iDeaths")));
        }
        return result;
    }

    private static Object property(Entity entity, String path) {
        return entity != null && entity.hasProperty(path) ? entity.getProperty(path) : null;
    }

    private static Integer integer(Entity entity, String path) {
        return property(entity, path) instanceof Number n ? n.intValue() : null;
    }

    Map<String, Object> artifact() {
        return artifact(roster());
    }

    Map<String, Object> artifact(List<Primary> eof) {
        if (!waiting.isEmpty()) throw new IllegalStateException("Combat events lack exact delivery-frame clocks: " + waiting.keySet());
        return ReplayProbe.mapOf(
                "hero_deaths", events, "rejected_hero_deaths", rejected,
                "excluded_nonhero_death_count", nonHeroDeaths,
                "excluded_illusion_death_count", illusionDeaths, "duplicate_count", duplicates,
                "eof_players", eof.stream().map(p -> ReplayProbe.mapOf(
                        "player_slot", p.slot, "team", p.team == 2 ? "RADIANT" : "DIRE",
                        "hero_id", p.heroId, "unit_name", p.unitName, "deaths", p.deaths,
                        "primary_identity_verified", p.verified, "active_clone_ambiguity", p.cloneAmbiguous
                )).toList()
        );
    }
}
