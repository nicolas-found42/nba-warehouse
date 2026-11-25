-- Compare reported game-end scores to curated finals; do not rewrite the feed.
-- Run read-only against v4. Legacy feeds that do not emit explicit end markers
-- are distinguished from games that have no play-by-play at all.
WITH markers AS (
    SELECT game_id, count(*) AS marker_count,
           count(DISTINCT (reported_home_score,reported_away_score)) AS distinct_scores,
           list(action_number ORDER BY action_number) AS marker_actions,
           list(struct_pack(home := reported_home_score, away := reported_away_score)
                ORDER BY action_number) AS reported_scores,
           min(reported_home_score) AS reported_home,
           min(reported_away_score) AS reported_away
    FROM nba.fact_play_event
    WHERE action_kind='game' AND lower(trim(source_subtype))='end'
      AND reported_home_score IS NOT NULL AND reported_away_score IS NOT NULL
    GROUP BY game_id
)
SELECT r.game_id, r.home_score, r.away_score, m.marker_actions, m.reported_scores,
       CASE WHEN a.published_events=0 THEN 'no_events'
            WHEN m.marker_count IS NULL THEN 'no_explicit_end_score'
            WHEN m.distinct_scores>1 THEN 'conflicting_end_markers'
            WHEN (m.reported_home,m.reported_away)<>(r.home_score,r.away_score) THEN 'score_disagrees'
            WHEN m.marker_count>1 THEN 'repeated_matching_end_marker'
            ELSE 'matches' END AS status
FROM nba.fact_game_result r JOIN audit.event_coverage a USING (game_id)
LEFT JOIN markers m USING (game_id);
