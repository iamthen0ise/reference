\set account random(1, 1000)
INSERT INTO events (account_id, payload) VALUES (:account, repeat(md5(random()::text), 8));
