from . import auth, fahrzeuge, live, route, saeulen

ALLE_ROUTER = [auth.router, fahrzeuge.router, route.router, saeulen.router,
               live.router]
