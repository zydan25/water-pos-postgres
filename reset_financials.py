from app import create_app
from database import get_db, financial_reset


def main():
    app = create_app()
    with app.app_context():
        db = get_db()
        backup_path = financial_reset(db, clear_opening_snapshots=True)
        db.commit()
        print(f'Financial data reset completed. Backup saved to: {backup_path}')


if __name__ == '__main__':
    main()
