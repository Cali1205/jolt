from . import auth, vehicles, live, push, route, chargers, cloud, logbook

ALL_ROUTER = [auth.router, vehicles.router, route.router, chargers.router,
               live.router, push.router, cloud.router, logbook.router]
