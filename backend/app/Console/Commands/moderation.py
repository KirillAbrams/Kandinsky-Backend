"""Читает и обрабатывает жалобы из командной строки."""

import argparse
import json
from typing import get_args
from uuid import UUID

from sqlalchemy import select

from app.Enums.moderation import ReportDecision
from app.Http.Resources.generation import GenerationReportResponse
from app.Models import (
    Asset,
    GenerationReport,
    Job,
)
from app.Services.Accounts.sessions import lock_user
from app.Services.Database.connection import database
from app.Services.Moderation.reports import resolve_report
from config.studio import Settings


# Разбирает команду просмотра, решения жалобы или восстановления доступа, открывает базу на время
# работы и закрывает её. Ограниченный список нужен, чтобы обычный просмотр не читал все жалобы
# сразу.
def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    listing = commands.add_parser("list", help="List the oldest unreviewed reports")
    listing.add_argument("--limit", type=int, default=25, help="Between 1 and 100")
    show = commands.add_parser("show", help="Show one report and its media path")
    show.add_argument("job", type=UUID)
    resolve = commands.add_parser("resolve", help="Record the decision for one report")
    resolve.add_argument("job", type=UUID)
    resolve.add_argument("decision", choices=get_args(ReportDecision))
    resolve.add_argument("--reviewer", required=True)
    resume = commands.add_parser("resume", help="Restore generation access for an account")
    resume.add_argument("owner", type=UUID)
    args = parser.parse_args()
    settings = Settings()
    engine, factory = database(settings.database_url)
    try:
        with factory() as db:
            if args.command == "list":
                if not 1 <= args.limit <= 100:
                    raise ValueError("Количество жалоб должно быть от 1 до 100")
                reports = db.scalars(
                    select(GenerationReport)
                    .where(GenerationReport.decision.is_(None))
                    .order_by(GenerationReport.created_at, GenerationReport.job_id)
                    .limit(args.limit)
                )
                output = [
                    GenerationReportResponse.model_validate(report).model_dump()
                    for report in reports
                ]
            elif args.command == "show":
                record = db.execute(
                    select(GenerationReport, Job, Asset)
                    .join(Job, Job.id == GenerationReport.job_id)
                    .outerjoin(Asset, (Asset.id == Job.output_id) & (Asset.user_id == Job.user_id))
                    .where(GenerationReport.job_id == str(args.job))
                ).one_or_none()
                if record is None:
                    raise ValueError("Жалоба не найдена")
                report, job, asset = record
                output = {
                    **GenerationReportResponse.model_validate(report).model_dump(),
                    "reviewer": report.reviewer,
                    "reviewed_at": report.reviewed_at,
                    "owner_id": job.user_id,
                    "model": job.model,
                    "media_path": str(settings.media_dir / asset.filename) if asset else None,
                }
            elif args.command == "resolve":
                report = resolve_report(
                    db, settings.media_dir, str(args.job), args.decision, args.reviewer
                )
                output = GenerationReportResponse.model_validate(report).model_dump()
            else:
                user = lock_user(db, id=str(args.owner))
                if user is None:
                    raise ValueError("Аккаунт не найден")
                user.generation_suspended_at = None
                db.commit()
                output = {"owner_id": user.id, "generation_suspended": False}
        print(json.dumps(output, ensure_ascii=False))
    except ValueError as error:
        parser.exit(2, str(error) + "\n")
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
