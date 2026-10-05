from dataclasses import dataclass

EPS = 1e-9


class InfeasibleRoute(Exception):
    pass


@dataclass(frozen=True)
class Candidate:
    index: int
    mile: float
    price: float
    detour: float = 0.0


@dataclass
class Stop:
    candidate: Candidate
    gallons: float
    cost: float
    arrival_fuel_miles: float
    fuel_after_miles: float


@dataclass
class Plan:
    stops: list
    total_cost: float
    total_gallons: float
    extra_detour_miles: float


def plan_fuel_stops(candidates, total_distance, usable_range, mpg, start_fuel_miles=None):
    if usable_range <= 0:
        raise ValueError('usable_range must be positive')
    fuel = usable_range if start_fuel_miles is None else min(start_fuel_miles, usable_range)

    # Start and destination are priced at 0, so the planner always prefers reaching the destination.
    nodes = [Candidate(-1, 0.0, 0.0)]
    nodes += sorted(candidates, key=lambda c: (c.mile, c.price))
    nodes.append(Candidate(-2, float(total_distance), 0.0))
    dest = len(nodes) - 1

    # A leg also burns fuel for each station's detour off the route (conservative: kept even if nothing is bought).
    def dist(i, j):
        return (nodes[j].mile - nodes[i].mile) + nodes[i].detour + nodes[j].detour

    stops, i = [], 0
    max_steps = len(nodes) + 5
    while i != dest:
        max_steps -= 1
        if max_steps < 0:
            raise InfeasibleRoute('Planner failed to converge.')
        if dist(i, dest) <= fuel + EPS:
            break

        limit = fuel if i == 0 else usable_range
        reach = [j for j in range(i + 1, dest + 1) if dist(i, j) <= limit + EPS]
        if not reach:
            gap = nodes[i + 1].mile - nodes[i].mile if i + 1 < len(nodes) else 0
            raise InfeasibleRoute(
                f'No fuel station within {usable_range:.0f} miles after mile '
                f'{nodes[i].mile:.0f} (next one is ~{gap:.0f} miles away).')

        cheaper = next((j for j in reach if nodes[j].price < nodes[i].price - EPS), None)
        arrival = fuel
        if cheaper is not None:
            nxt = cheaper
            buy_miles = max(0.0, dist(i, nxt) - fuel)
        else:
            buy_miles = usable_range - fuel
            nxt = min(reach, key=lambda j: (nodes[j].price, -nodes[j].mile))

        if buy_miles > EPS and i != 0:
            gallons = buy_miles / mpg
            stops.append(Stop(nodes[i], gallons, gallons * nodes[i].price,
                              arrival, arrival + buy_miles))
        fuel += buy_miles if i != 0 else 0.0
        fuel -= dist(i, nxt)
        i = nxt

    return Plan(
        stops=stops,
        total_cost=sum(s.cost for s in stops),
        total_gallons=sum(s.gallons for s in stops),
        extra_detour_miles=sum(2 * s.candidate.detour for s in stops),
    )
