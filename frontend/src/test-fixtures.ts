import type { ComparisonItem, Pick, Recommendation, RunView, Step } from './api/client'

export function pick(over: Partial<Pick> = {}): Pick {
  return {
    match_tier: 'equivalent',
    match_confidence: 'high',
    product_name: 'Coles Beef Mince 500g',
    brand: 'Coles',
    brand_label: 'house_brand',
    pack_size: '500',
    pack_unit: 'g',
    packs_needed: 1,
    purchased_qty: '500',
    leftover_qty: '0',
    price: '6.50',
    regular_price: '6.50',
    total_cost: '6.50',
    regular_total_cost: '6.50',
    on_special: false,
    special_end_date: null,
    reason: 'Best value.',
    ...over,
  }
}

export function item(id: string, name: string, coles: Partial<Pick>, woolworths: Partial<Pick>): ComparisonItem {
  return {
    item_id: id,
    name,
    quantity: '500',
    unit: 'g',
    stores: { coles: pick(coles), woolworths: pick({ product_name: `Woolworths ${name}`, ...woolworths }) },
  }
}

export function recommendation(over: Partial<Recommendation> = {}): Recommendation {
  return {
    recommendation: 'no_meaningful_difference',
    store: null,
    split_assignment: null,
    savings: '0',
    summary_line: 'No meaningful difference: Woolworths is $1.00 cheaper on 2 common items (threshold $8.00).',
    reasoning: 'The two stores are within a dollar of each other.',
    confidence: 'high',
    overridden: false,
    override_reason: null,
    tips: [],
    recheck_items: [],
    assumptions: ['Prices are as listed.'],
    flags: [],
    model: 'gemini-3.1-flash-lite',
    fell_back: false,
    prompt_version: '4',
    rule_verdict: {
      kind: 'no_meaningful_difference',
      store: null,
      totals: { coles: '13.00', woolworths: '12.00' },
      gap: '1.00',
      threshold: '8.00',
      split_savings: '0.20',
      condition: null,
    },
    ...over,
  }
}

export function run(over: Partial<RunView> = {}): RunView {
  return {
    id: '3a1f9a52-0000-4000-8000-000000000001',
    status: 'completed',
    stop_reason: null,
    failure_reason: null,
    message: null,
    created_at: '2026-10-19T01:00:00Z',
    completed_at: '2026-10-19T01:00:12Z',
    request: { meals: [{ meal_name: 'Dinner', mains: ['mince'], sides: [], people: 2, days: 1 }], include_pantry_staples: false },
    parameters: { config: {}, as_of: '2026-10-19', stores: ['coles', 'woolworths'], prompt_versions: {} },
    ingredient_list: {
      items: [
        { item_id: 'i1', name: 'beef mince', quantity: '500', unit: 'g', category: 'meat', meals: ['Dinner'], brand: null, pantry_staple: false, in_vocabulary: true, flags: [] },
        { item_id: 'i2', name: 'eggs', quantity: '12', unit: 'each', category: 'dairy', meals: ['Dinner', 'Brunch'], brand: null, pantry_staple: false, in_vocabulary: true, flags: [] },
      ],
      assumptions: ['Dinner: assumed 250 g of mince each.'],
    },
    comparison: [
      item('i1', 'beef mince', { total_cost: '6.50' }, { total_cost: '6.80' }),
      item('i2', 'eggs', { total_cost: '6.50', product_name: 'Coles Eggs 12 pack' }, { total_cost: '5.20' }),
    ],
    recommendation: recommendation(),
    totals: {
      requests: 3,
      tokens_in: 5000,
      tokens_out: 900,
      thinking_tokens: 0,
      latency_ms: 11100,
      models: ['gemini-3.1-flash-lite'],
      fell_back: false,
      loopback_used: false,
      would_cost_usd: '0.0028',
    },
    ...over,
  }
}

export const oneStore = () =>
  run({
    recommendation: recommendation({
      recommendation: 'one_store',
      store: 'woolworths',
      savings: '18.42',
      summary_line: 'Shop at Woolworths: $18.42 cheaper than Coles on 2 common items (threshold $8.00).',
      rule_verdict: { kind: 'one_store', store: 'woolworths', totals: { coles: '116.52', woolworths: '98.10' }, gap: '18.42', threshold: '8.00', split_savings: '0.00', condition: null },
    }),
  })

export const conditional = () =>
  run({
    recommendation: recommendation({
      recommendation: 'one_store',
      store: 'woolworths',
      savings: '11.40',
      summary_line:
        'Woolworths is $11.40 cheaper if you shop by Tue 20 Oct (beef mince special); after that, no meaningful difference.',
      flags: [{ kind: 'special_ending', item_id: 'i1', store: 'woolworths', ends_on: '2026-10-20', ends_in_days: 1, message: 'beef mince is on special at woolworths until 2026-10-20 (1 days from now)' }],
      rule_verdict: {
        kind: 'one_store',
        store: 'woolworths',
        totals: { coles: '97.10', woolworths: '85.70' },
        gap: '11.40',
        threshold: '8.00',
        split_savings: '0.70',
        condition: {
          valid_until: '2026-10-20',
          items: ['i1'],
          fallback: { kind: 'no_meaningful_difference', store: null, totals: { coles: '97.10', woolworths: '95.70' }, gap: '1.40', threshold: '8.00', split_savings: '0.70' },
        },
      },
    }),
    comparison: [
      item('i1', 'beef mince', { total_cost: '52.00' }, { total_cost: '42.00', on_special: true, special_end_date: '2026-10-20', regular_total_cost: '52.00' }),
    ],
  })

export const split = () =>
  run({
    recommendation: recommendation({
      recommendation: 'split',
      savings: '10.00',
      split_assignment: { i1: 'woolworths', i2: 'coles' },
      summary_line: 'Splitting the shop (1 of 2 common items bought away from Coles) saves $10.00 (threshold $8.00).',
      rule_verdict: { kind: 'split', store: null, totals: { coles: '97.10', woolworths: '100.12' }, gap: '3.02', threshold: '8.00', split_savings: '10.00', condition: null },
    }),
  })

export const withFlags = () =>
  run({
    comparison: [
      item('i1', 'beef mince', { total_cost: '6.50' }, { match_tier: 'substitute', product_name: 'Woolworths Diced Beef', total_cost: '9.40' }),
      item('i2', 'sourdough', { match_tier: 'not_found', product_name: null, total_cost: null, reason: 'Coles does not sell sourdough.' }, { total_cost: '5.50' }),
      item('i3', 'bacon', { brand_label: 'branded', on_special: true, special_end_date: '2026-10-20', total_cost: '4.50', regular_total_cost: '7.00' }, { total_cost: '5.80', match_confidence: 'low' }),
    ],
    recommendation: recommendation({
      flags: [
        { kind: 'tier_gap', item_id: 'i3', message: 'bacon: the stores picks differ in brand label' },
        { kind: 'pack_gap', item_id: 'i1', message: 'beef mince: the stores sell different amounts' },
        { kind: 'missing', item_id: 'i2', store: 'coles', message: 'sourdough has no usable match at coles, so it is left out of the comparison' },
      ],
      tips: [{ item_id: 'i3', text: 'Bacon is cheaper at Coles while the special lasts.' }],
      confidence: 'medium',
      overridden: true,
      override_reason: 'match_quality',
    }),
  })

export const stopped = (): RunView =>
  run({
    status: 'stopped',
    stop_reason: 'too_many_unmatched',
    message: "We couldn't match enough of your list to compare the stores: 3 of 4 items have no usable match at any store. Your ingredient list is below, so you can still shop from it.",
    recommendation: null,
    comparison: [],
  })

export const failed = (): RunView =>
  run({
    status: 'failed',
    failure_reason: 'model_unavailable',
    message: 'The AI service is unavailable right now. Please try again later.',
    recommendation: null,
    ingredient_list: null,
    comparison: [],
  })

export const stopSteps = (): Step[] => [
  {
    seq: 3,
    name: 'stop',
    agent: null,
    attempt: 1,
    status: 'completed',
    summary: 'stopped: too_many_unmatched',
    input: null,
    output: {
      failed_items: [
        {
          item_id: 'i1',
          name: 'dragon fruit',
          quantity: '500',
          unit: 'g',
          found_nowhere: true,
          stores: {
            coles: { match_tier: 'not_found', product: null, reason: 'No dragon fruit in the catalogue.' },
            woolworths: { match_tier: 'substitute', product: 'Woolworths Royal Gala Apples 1kg', reason: 'Closest fruit.' },
          },
        },
      ],
    },
    tool_calls: [],
    requests: 0,
    tokens_in: 0,
    tokens_out: 0,
    thinking_tokens: 0,
    fell_back: false,
    started_at: '2026-10-19T01:00:00Z',
  },
]
