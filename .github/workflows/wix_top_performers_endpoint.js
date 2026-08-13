// Add this to your existing backend/http-functions.js, alongside
// post_leaderboards. Reachable at:
//   https://www.yankeesfarmreport.com/_functions/top_performers
//
// Before this will work you need to (manual UI step, same as your other
// collections -- Wix Data schema changes can't be done via API):
//   1. Content Manager -> Create Collection -> name it "TopPerformers"
//   2. Add fields: date (Text), generatedAt (Date/Time),
//      hitters (Array), pitchers (Array)
//   3. Secrets Manager -> confirm the secret name matches whatever your
//      existing push_to_wix.py setup uses (this assumes the same one:
//      YANKEESFARM_PUSH_SECRET)

import { ok, badRequest, serverError } from 'wix-http-functions';
import { getSecret } from 'wix-secrets-backend';
import wixData from 'wix-data';

export async function post_top_performers(request) {
    let body;
    try {
        body = await request.body.json();
    } catch (e) {
        return badRequest({ body: { error: 'Invalid JSON body' } });
    }

    const providedSecret = request.headers['x-push-secret'];
    const expectedSecret = await getSecret('YANKEESFARM_PUSH_SECRET');
    if (!providedSecret || providedSecret !== expectedSecret) {
        return badRequest({ body: { error: 'Unauthorized' } });
    }

    if (!Array.isArray(body.hitters) || !Array.isArray(body.pitchers)) {
        return badRequest({ body: { error: 'Payload must include hitters[] and pitchers[]' } });
    }

    try {
        // Fixed _id 'latest' -- upserts a single snapshot document, same
        // pattern already used for FarmAnalyticsSnapshot. The page just
        // always reads whatever's in 'latest'.
        await wixData.save('TopPerformers', {
            _id: 'latest',
            date: body.date,
            generatedAt: body.generatedAt,
            hitters: body.hitters,
            pitchers: body.pitchers
        }, { suppressAuth: true });

        return ok({ body: { status: 'saved', date: body.date } });
    } catch (e) {
        return serverError({ body: { error: e.message } });
    }
}
