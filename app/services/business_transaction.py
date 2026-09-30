"""Keep an approved command and its audit result in one caller-owned transaction."""
def commit_business_change(db):
    if db.info.get("business_approval_transaction"):
        db.flush()
    else:
        db.commit()
