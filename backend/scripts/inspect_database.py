"""Read-only schema, constraints, indexes, RLS, and row-count audit."""

from sqlalchemy import BigInteger, Boolean, Date, DateTime, Float, Integer, Numeric, String, Text, UniqueConstraint, inspect, text
from sqlalchemy.exc import SAWarning
import warnings

from backend.app.database import engine
from backend.app.models import Base


def type_family(value):
    if isinstance(value, (String, Text)):
        return "string"
    if isinstance(value, (Integer, BigInteger)):
        return "integer"
    if isinstance(value, (Float, Numeric)):
        return "numeric"
    if isinstance(value, DateTime):
        return "datetime"
    if isinstance(value, Date):
        return "date"
    if isinstance(value, Boolean):
        return "boolean"
    return type(value).__name__.lower()


def main() -> int:
    inspector = inspect(engine)
    print(f"Database dialect: {engine.dialect.name} ({engine.dialect.driver})")
    tables = set(inspector.get_table_names())
    expected = set(Base.metadata.tables)
    print("Tables:")
    for table in sorted(tables):
        print(f"\n[{table}]")
        columns = inspector.get_columns(table)
        actual_names = {column["name"] for column in columns}
        for column in columns:
            nullable = "nullable" if column["nullable"] else "not null"
            default = f" default={column['default']}" if column.get("default") is not None else ""
            print(f"  column {column['name']}: {column['type']} {nullable}{default}")
        print(f"  primary key: {inspector.get_pk_constraint(table).get('constrained_columns')}")
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", SAWarning)
            reflected_unique_constraints = inspector.get_unique_constraints(table)
        for constraint in reflected_unique_constraints:
            print(f"  unique: {constraint.get('column_names')}")
        for constraint in inspector.get_foreign_keys(table):
            print(f"  foreign key: {constraint.get('constrained_columns')} -> {constraint.get('referred_table')}.{constraint.get('referred_columns')}")
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", SAWarning)
            reflected_indexes = inspector.get_indexes(table)
        for index in reflected_indexes:
            print(f"  index: {index.get('name')} ({index.get('column_names')}) unique={index.get('unique')}")
        sqlite_index_names = {item.get("name") for item in reflected_indexes}
        if engine.dialect.name == "sqlite":
            with engine.connect() as connection:
                pragma_indexes = connection.exec_driver_sql(
                    f"PRAGMA index_list({engine.dialect.identifier_preparer.quote(table)})"
                ).all()
            for row in pragma_indexes:
                if row[1] not in sqlite_index_names:
                    print(f"  index: {row[1]} (expression) unique={bool(row[2])}")
        model = Base.metadata.tables.get(table)
        if model is not None:
            for name in sorted(set(model.columns.keys()) - actual_names):
                print(f"  MODEL DRIFT: missing column required by this app: {name}")
            actual_by_name = {column["name"]: column for column in columns}
            for column in model.columns:
                actual_column = actual_by_name.get(column.name)
                if actual_column is None:
                    continue
                if type_family(column.type) != type_family(actual_column["type"]):
                    print(f"  MODEL DRIFT: {column.name} type expected={column.type} actual={actual_column['type']}")
                if bool(column.nullable) != bool(actual_column["nullable"]):
                    print(f"  MODEL DRIFT: {column.name} nullability expected={column.nullable} actual={actual_column['nullable']}")
                if engine.dialect.name == "postgresql" and isinstance(column.type, DateTime):
                    if bool(getattr(column.type, "timezone", False)) != bool(getattr(actual_column["type"], "timezone", False)):
                        print(f"  MODEL DRIFT: {column.name} timezone-aware timestamp mismatch")

            expected_pk = tuple(column.name for column in model.primary_key.columns)
            actual_pk = tuple(inspector.get_pk_constraint(table).get("constrained_columns") or ())
            if set(expected_pk) != set(actual_pk):
                print(f"  MODEL DRIFT: primary key expected={expected_pk} actual={actual_pk}")

            expected_fk = {
                (tuple(column.name for column in constraint.columns),
                 constraint.elements[0].column.table.name,
                 tuple(element.column.name for element in constraint.elements))
                for constraint in model.foreign_key_constraints
            }
            actual_fk = {
                (tuple(item.get("constrained_columns") or ()), item.get("referred_table"), tuple(item.get("referred_columns") or ()))
                for item in inspector.get_foreign_keys(table)
            }
            for key in sorted(expected_fk - actual_fk, key=str):
                print(f"  MODEL DRIFT: missing foreign key {key}")

            expected_unique = {
                tuple(sorted(constraint.columns.keys()))
                for constraint in model.constraints if isinstance(constraint, UniqueConstraint)
            }
            actual_unique = {
                tuple(sorted(item.get("column_names") or ()))
                for item in inspector.get_unique_constraints(table)
            }
            actual_unique.update(
                tuple(sorted(item.get("column_names") or ()))
                for item in inspector.get_indexes(table) if item.get("unique")
            )
            for key in sorted(expected_unique - actual_unique):
                print(f"  MODEL DRIFT: missing unique constraint/index on {key}")

            model_indexes = {index.name: index for index in model.indexes}
            actual_indexes = {item.get("name"): item for item in inspector.get_indexes(table)}
            # SQLite's SQLAlchemy inspector omits expression indexes. Read
            # their names/uniqueness from PRAGMA so normalized-ID indexes are
            # not reported as missing merely because they use lower(trim(...)).
            if engine.dialect.name == "sqlite":
                with engine.connect() as connection:
                    sqlite_indexes = connection.exec_driver_sql(
                        f"PRAGMA index_list({engine.dialect.identifier_preparer.quote(table)})"
                    ).all()
                actual_indexes.update({
                    row[1]: {"name": row[1], "unique": bool(row[2])}
                    for row in sqlite_indexes
                })
            for name, index in sorted(model_indexes.items()):
                actual = actual_indexes.get(name)
                if actual is None:
                    print(f"  MODEL DRIFT: missing index {name}")
                elif bool(index.unique) != bool(actual.get("unique")):
                    print(f"  MODEL DRIFT: index {name} uniqueness expected={index.unique} actual={actual.get('unique')}")
        quoted = engine.dialect.identifier_preparer.quote(table)
        with engine.connect() as connection:
            count = connection.execute(text(f"SELECT count(*) FROM {quoted}")).scalar_one()
        print(f"  row count: {count} (contents not read)")

    missing_tables = sorted(expected - tables)
    if missing_tables:
        print("\nMODEL DRIFT: missing application tables: " + ", ".join(missing_tables))

    if engine.dialect.name == "postgresql":
        with engine.connect() as connection:
            policies = connection.execute(text("""
                SELECT schemaname, tablename, policyname, roles, cmd, qual, with_check
                FROM pg_policies WHERE schemaname = current_schema()
                ORDER BY tablename, policyname
            """)).mappings().all()
            rls = connection.execute(text("""
                SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity
                FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
                WHERE n.nspname = current_schema() AND c.relkind = 'r'
                ORDER BY c.relname
            """)).mappings().all()
        print("\nRow-level security:")
        for row in rls:
            print(f"  {row['relname']}: enabled={row['relrowsecurity']} forced={row['relforcerowsecurity']}")
        print("Policies:")
        if not policies:
            print("  none")
        for row in policies:
            print(f"  {row['tablename']}.{row['policyname']}: roles={row['roles']} command={row['cmd']} using={row['qual']} check={row['with_check']}")
    return 0 if not missing_tables else 2


if __name__ == "__main__":
    raise SystemExit(main())
