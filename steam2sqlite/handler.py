import asyncio
import sqlite3
from datetime import datetime

import httpx
import sqlalchemy.exc
from rich import print
from sqlmodel import Session, select

from steam2sqlite import ACHIEVEMENT_URL, APPID_URL, BATCH_SIZE, navigator, utils
from steam2sqlite.models import Achievement, AppidError, Category, Genre, SteamApp


class DataParsingError(Exception):
    def __init__(self, appid: int, reason: str | None = None):
        self.appid = appid
        self.reason = reason


def get_or_create(session, model, **kwargs):
    instance = session.query(model).filter_by(**kwargs).first()
    if instance:
        return instance
    else:
        instance = model(**kwargs)
        session.add(instance)
        session.flush()
        return instance


async def get_app_achievements(client: httpx.AsyncClient, appid: int) -> list[dict]:
    url = ACHIEVEMENT_URL.format(appid)
    resp = await navigator.get(client, url, headers={"accept": "application/json"})
    try:
        resp.raise_for_status()
        data = resp.json()
        if (
            "achievementpercentages" in data
            and "achievements" in data["achievementpercentages"]
        ):
            return data["achievementpercentages"]["achievements"]
    except (httpx.TimeoutException, httpx.HTTPError) as e:
        print(f"Error getting achievements for appid: {appid}, {e}")
        # todo: log this error in db
        pass
    return []


async def get_apps_achievements(apps: list[SteamApp]) -> list[list[dict]]:

    limits = httpx.Limits(max_keepalive_connections=5, max_connections=10)
    async with httpx.AsyncClient(
        headers={"accept": "application/json"}, timeout=10, limits=limits
    ) as client:
        tasks = [get_app_achievements(client, app.appid) for app in apps]
        achievements = await asyncio.gather(*tasks)
    return achievements  # type: ignore


def store_apps_achievements(
    session: Session, apps: list[SteamApp], achievements_dict: list[list[dict]]
) -> None:

    for app, app_achievements_dict in zip(apps, achievements_dict):
        for achievement_dict in app_achievements_dict:
            achievement_args = achievement_dict | {"steam_app": app}
            get_or_create(session, Achievement, **achievement_args)

        session.commit()


def load_into_db(session: Session, data: dict) -> SteamApp:

    genres_data = data.get("genres") or []
    if genres_data:
        # deduplicate
        genres_data = list({v["id"]: v for v in genres_data}.values())
    genres = [get_or_create(session, Genre, **dd) for dd in genres_data]

    categories_data = data.get("categories") or []
    if categories_data:
        # deduplicate
        categories_data = list({v["id"]: v for v in categories_data}.values())
    categories = [get_or_create(session, Category, **dd) for dd in categories_data]

    metacritic_score, metacritic_url = None, None
    if "metacritic" in data:
        metacritic_score = data["metacritic"].get("score")
        metacritic_url = data["metacritic"].get("url")

    recommendations_total = None
    if "recommendations" in data:
        recommendations_total = data["recommendations"].get("total")

    achievements_total = 0
    if "achievements" in data:
        achievements_total = data["achievements"].get("total", 0)

    release_date = None
    if "release_date" in data and not (
        "coming_soon" in data["release_date"] and data["release_date"]["coming_soon"]
    ):
        release_date_str = data["release_date"].get("date")
        try:
            if release_date_str:
                release_date = datetime.strptime(release_date_str, "%b %d, %Y").date()
        except ValueError:
            # todo: log this error
            pass

    app_attrs = {
        "appid": data["steam_appid"],
        "type": data["type"],
        "is_free": data.get("is_free"),
        "name": data["name"],
        "controller_support": data.get("controller_support"),
        "metacritic_score": metacritic_score,
        "metacritic_url": metacritic_url,
        "recommendations": recommendations_total,
        "achievements_total": achievements_total,
        "release_date": release_date,
    }
    steam_app = session.exec(
        select(SteamApp).where(SteamApp.appid == data["steam_appid"])
    ).one_or_none()
    if steam_app:  # update
        for key, value in app_attrs.items():
            setattr(steam_app, key, value)
    else:  # create
        steam_app = SteamApp(**app_attrs)

    steam_app.categories = categories
    steam_app.genres = genres

    session.add(steam_app)
    session.commit()
    session.refresh(steam_app)

    return steam_app


def import_single_item(session: Session, item: dict) -> SteamApp | None:

    appid = list(item.keys())[0]
    if item[appid]["success"] is False:
        raise DataParsingError(int(appid), reason="Response from api: success=False")

    data = item[appid]["data"]

    if int(appid) != data["steam_appid"]:
        raise DataParsingError(
            int(appid),
            reason=f"duplicate entry with current appid {appid} and steam appid: {data['steam_appid']}",
        )

    try:
        app = load_into_db(session, data)
    except (sqlite3.DatabaseError, sqlalchemy.exc.IntegrityError) as e:
        raise DataParsingError(int(appid), reason=f"Database error: {e}")

    return app


def get_appids_from_db(session: Session) -> list[tuple[int, datetime]]:
    return session.exec(
        select(SteamApp.appid, SteamApp.updated).order_by(SteamApp.updated.asc())  # type: ignore
    ).all()


def get_error_appids(session: Session) -> list[int]:
    return session.exec(select(AppidError.appid)).all()


def record_appid_error(
    session, appid: int, name: str | None = None, reason: str | None = None
):
    get_or_create(
        session, AppidError, **{"appid": appid, "name": name, "reason": reason}
    )
    session.commit()


# delay by 10 seconds for rate limiting
@utils.delay_by(BATCH_SIZE)
def get_apps_data(
    session: Session, steam_appids_names: dict[int, str], appids: list[str]
) -> list[dict]:

    urls = [APPID_URL.format(appid) for appid in appids if appid is not None]
    responses = asyncio.run(navigator.make_requests(urls))

    apps_data = []
    for appid, resp in zip(appids, responses):
        try:
            resp.raise_for_status()
            item = resp.json()
            apps_data.append(item)

        except httpx.HTTPError as e:
            print(e)
            record_appid_error(session, appid, steam_appids_names[appid], e.reason)

    return apps_data


def store_apps_data(
    session: Session, steam_appids_names: dict[int, str], apps_data: list[dict]
) -> list[SteamApp]:
    apps = []
    for app_data in apps_data:
        try:
            app = import_single_item(session, app_data)
            apps.append(app)
        except DataParsingError as e:
            # todo: log the error instead of print
            print(f"Error for appid: {e.appid}, reason: {e.reason}")
            record_appid_error(
                session, e.appid, steam_appids_names.get(e.appid, 0), e.reason
            )
    return apps
