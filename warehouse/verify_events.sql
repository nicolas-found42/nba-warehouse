-- Re-run against a closed release; these checks do not attach the source.
WITH event_published AS (
    SELECT game_id,count(*) n,count(*) FILTER (WHERE actor_kind='unresolved') unknowns
    FROM nba.fact_play_event GROUP BY 1
), event_held AS (
    SELECT game_id,count(*) n FROM audit.event_hold GROUP BY 1
), shot_published AS (
    SELECT game_id,count(*) n FROM nba.fact_shot GROUP BY 1
), shot_held AS (
    SELECT game_id,sum(multiplicity) n FROM audit.shot_hold GROUP BY 1
), shot_matched AS (
    SELECT game_id,count(*) n FROM verify_shot_event_links GROUP BY 1
), checks(check_name,violations) AS (
    SELECT 'event_game_coverage_includes_spine',count(*)
    FROM nba.dim_game g ANTI JOIN audit.event_coverage a USING (game_id)
    UNION ALL SELECT 'event_accounting_matches_materialized',count(*)
    FROM audit.event_coverage a LEFT JOIN event_published p USING (game_id) LEFT JOIN event_held h USING (game_id)
    WHERE a.published_events<>coalesce(p.n,0) OR a.held_events<>coalesce(h.n,0)
       OR a.unresolved_actors<>coalesce(p.unknowns,0)
    UNION ALL SELECT 'event_holds_disjoint_from_publication',count(*)
    FROM audit.event_hold a JOIN nba.fact_play_event e USING (game_id,action_number)
    UNION ALL SELECT 'event_person_has_correct_team',count(*)
    FROM nba.fact_play_event e JOIN nba.fact_player_game_stats p USING (game_id,person_id)
    WHERE e.actor_kind='person' AND e.team_id IS DISTINCT FROM p.team_id
    UNION ALL SELECT 'event_zero_action_key_can_be_valid',CASE WHEN count(*)>0 THEN 0 ELSE 1 END
    FROM nba.fact_play_event WHERE action_number=0
    UNION ALL SELECT 'shot_coverage_includes_spine',count(*)
    FROM nba.dim_game g ANTI JOIN audit.shot_coverage a USING (game_id)
    UNION ALL SELECT 'shot_accounting_matches_materialized',count(*)
    FROM audit.shot_coverage a LEFT JOIN shot_published p USING (game_id)
    LEFT JOIN shot_held h USING (game_id) LEFT JOIN shot_matched m USING (game_id)
    WHERE a.published_shots<>coalesce(p.n,0) OR a.held_rows<>coalesce(h.n,0)
       OR a.matched_shots<>coalesce(m.n,0)
    UNION ALL SELECT 'shot_holds_disjoint_from_publication',count(*)
    FROM audit.shot_hold a JOIN nba.fact_shot s USING (shot_key)
    UNION ALL SELECT 'ambiguous_shots_not_linked',count(*)
    FROM audit.shot_match_ambiguous a LEFT JOIN nba.fact_shot s USING (shot_key)
    LEFT JOIN verify_shot_event_links b USING (shot_key)
    WHERE s.shot_key IS NULL OR b.shot_key IS NOT NULL
    UNION ALL SELECT 'shot_person_has_correct_team',count(*)
    FROM nba.fact_shot s JOIN nba.fact_player_game_stats p USING (game_id,person_id)
    WHERE s.team_id IS DISTINCT FROM p.team_id
    UNION ALL SELECT 'shot_links_align_game_person_team_clock_outcome_value_geometry',count(*)
    FROM nba.bridge_shot_event l JOIN nba.fact_shot s USING (shot_key)
    JOIN nba.fact_play_event p ON (l.game_id,l.action_number)=(p.game_id,p.action_number)
    WHERE s.game_id IS DISTINCT FROM p.game_id OR s.team_id IS DISTINCT FROM p.team_id
       OR s.person_id IS DISTINCT FROM p.person_id OR s.period IS DISTINCT FROM p.period
       OR s.seconds_remaining IS DISTINCT FROM floor(p.seconds_remaining)::INTEGER
       OR s.made IS DISTINCT FROM p.shot_made OR s.shot_value IS DISTINCT FROM p.field_goal_value
       OR p.is_field_goal IS NOT TRUE OR s.geometry_status<>'reported'
       OR s.x_tenths_of_foot IS DISTINCT FROM l.x_legacy OR s.y_tenths_of_foot IS DISTINCT FROM l.y_legacy
    UNION ALL SELECT 'shot_links_unique_both_directions',count(*) FROM (
       SELECT shot_key FROM verify_shot_event_links GROUP BY 1 HAVING count(*)<>1
       UNION ALL
       SELECT NULL::BLOB FROM verify_shot_event_links GROUP BY game_id,action_number HAVING count(*)<>1
    )
    UNION ALL SELECT 'events_and_shots_and_links_are_populated', CASE WHEN
       (SELECT count(*) FROM nba.fact_play_event)>0 AND (SELECT count(*) FROM nba.fact_shot)>0
        AND (SELECT count(*) FROM nba.bridge_shot_event)>0 THEN 0 ELSE 1 END
)
SELECT check_name, violations FROM checks ORDER BY check_name;
