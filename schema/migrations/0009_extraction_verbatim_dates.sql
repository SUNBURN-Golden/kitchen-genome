-- Keep source evidence separate from the normalized date used by consumers.
ALTER TABLE extraction ADD COLUMN date_iso TEXT;
UPDATE extraction SET date_iso=date_text
 WHERE extractor LIKE 'rule:%' AND date_text GLOB '[0-9][0-9][0-9][0-9]*';
-- Repair already queued rule rechecks from their original structured finding.
-- Model findings are deliberately excluded: their evidence is never repaired.
UPDATE extraction SET date_text=(
 SELECT json_extract(q.payload_json,'$.finding.date_text') FROM review_queue q
 WHERE q.subject_type='extraction' AND q.subject_id=extraction.extraction_id
   AND q.reason='ai_inference' AND q.state='open'
 LIMIT 1)
 WHERE workload='recheck' AND extractor LIKE 'rule:%' AND EXISTS (
 SELECT 1 FROM review_queue q WHERE q.subject_type='extraction'
   AND q.subject_id=extraction.extraction_id AND q.reason='ai_inference'
   AND q.state='open' AND json_type(q.payload_json,'$.finding.date_text')='text');
