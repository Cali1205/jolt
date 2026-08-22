"""Baseline: das vollstaendige Schema von jolt.

Alle spaeteren Aenderungen kommen als eigene Revision dazu. Ein create_all
gibt es bewusst nicht - zwei Quellen fuer dasselbe Schema laufen unweigerlich
auseinander, und der Unterschied faellt erst in der Produktion auf.

Revision ID: 0001
Revises:
"""
from alembic import op
import sqlalchemy as sa


revision = '0001'
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('fahrzeuge',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('name', sa.String(length=120), nullable=False),
    sa.Column('akku_brutto_kwh', sa.Float(), nullable=False),
    sa.Column('akku_netto_kwh', sa.Float(), nullable=False),
    sa.Column('leermasse_kg', sa.Float(), nullable=False),
    sa.Column('zuladung_kg', sa.Float(), nullable=False),
    sa.Column('c_w', sa.Float(), nullable=False),
    sa.Column('stirnflaeche_m2', sa.Float(), nullable=False),
    sa.Column('c_rr', sa.Float(), nullable=False),
    sa.Column('eta_antrieb', sa.Float(), nullable=False),
    sa.Column('eta_rekup', sa.Float(), nullable=False),
    sa.Column('p_neben_w', sa.Float(), nullable=False),
    sa.Column('waermepumpe', sa.Boolean(), nullable=False),
    sa.Column('reserve_soc', sa.Float(), nullable=False),
    sa.Column('ziel_soc', sa.Float(), nullable=False),
    sa.Column('max_ladeleistung_kw', sa.Float(), nullable=False),
    sa.Column('steckertyp', sa.String(length=20), nullable=False),
    sa.Column('korrekturfaktor', sa.Float(), nullable=False),
    sa.Column('angelegt', sa.DateTime(), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('ladepunkte',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('quelle', sa.String(length=20), nullable=False),
    sa.Column('fremd_id', sa.String(length=80), nullable=False),
    sa.Column('name', sa.String(length=200), nullable=True),
    sa.Column('betreiber', sa.String(length=200), nullable=True),
    sa.Column('lat', sa.Float(), nullable=False),
    sa.Column('lon', sa.Float(), nullable=False),
    sa.Column('adresse', sa.String(length=250), nullable=True),
    sa.Column('plz', sa.String(length=20), nullable=True),
    sa.Column('ort', sa.String(length=120), nullable=True),
    sa.Column('land', sa.String(length=2), nullable=True),
    sa.Column('anschluesse', sa.JSON(), nullable=True),
    sa.Column('max_kw', sa.Float(), nullable=False),
    sa.Column('anzahl_punkte', sa.Integer(), nullable=False),
    sa.Column('steckertypen', sa.String(length=120), nullable=True),
    sa.Column('stand', sa.String(length=20), nullable=True),
    sa.Column('aktualisiert', sa.DateTime(), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('quelle', 'fremd_id', name='uq_ladepunkt_quelle')
    )
    with op.batch_alter_table('ladepunkte', schema=None) as batch_op:
        batch_op.create_index('ix_ladepunkt_kw', ['max_kw'], unique=False)
        batch_op.create_index('ix_ladepunkt_pos', ['lat', 'lon'], unique=False)

    op.create_table('sitzungen',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('token', sa.String(length=64), nullable=False),
    sa.Column('geraet', sa.String(length=120), nullable=True),
    sa.Column('erstellt', sa.DateTime(), nullable=False),
    sa.Column('zuletzt_gesehen', sa.DateTime(), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('sitzungen', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_sitzungen_token'), ['token'], unique=True)

    op.create_table('fahrten',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('fahrzeug_id', sa.Integer(), nullable=False),
    sa.Column('start_text', sa.String(length=250), nullable=True),
    sa.Column('start_lat', sa.Float(), nullable=False),
    sa.Column('start_lon', sa.Float(), nullable=False),
    sa.Column('ziel_text', sa.String(length=250), nullable=True),
    sa.Column('ziel_lat', sa.Float(), nullable=False),
    sa.Column('ziel_lon', sa.Float(), nullable=False),
    sa.Column('start_soc', sa.Float(), nullable=False),
    sa.Column('tempo_faktor', sa.Float(), nullable=False),
    sa.Column('aussentemp_c', sa.Float(), nullable=True),
    sa.Column('strecke_m', sa.Float(), nullable=True),
    sa.Column('fahrzeit_s', sa.Float(), nullable=True),
    sa.Column('geometrie', sa.JSON(), nullable=True),
    sa.Column('energieprofil', sa.JSON(), nullable=True),
    sa.Column('angelegt', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['fahrzeug_id'], ['fahrzeuge.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('ladekurvenpunkte',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('fahrzeug_id', sa.Integer(), nullable=False),
    sa.Column('soc_prozent', sa.Float(), nullable=False),
    sa.Column('kw', sa.Float(), nullable=False),
    sa.ForeignKeyConstraint(['fahrzeug_id'], ['fahrzeuge.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('fahrzeug_id', 'soc_prozent', name='uq_ladekurve_soc')
    )
    with op.batch_alter_table('ladekurvenpunkte', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_ladekurvenpunkte_fahrzeug_id'), ['fahrzeug_id'], unique=False)

    op.create_table('live_sitzungen',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('fahrt_id', sa.Integer(), nullable=False),
    sa.Column('gestartet', sa.DateTime(), nullable=False),
    sa.Column('beendet', sa.DateTime(), nullable=True),
    sa.Column('laeuft', sa.Boolean(), nullable=False),
    sa.Column('verbrauchsfaktor', sa.Float(), nullable=False),
    sa.Column('hinweis', sa.Text(), nullable=True),
    sa.ForeignKeyConstraint(['fahrt_id'], ['fahrten.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('live_sitzungen', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_live_sitzungen_fahrt_id'), ['fahrt_id'], unique=False)

    op.create_table('live_punkte',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('sitzung_id', sa.Integer(), nullable=False),
    sa.Column('zeit', sa.DateTime(), nullable=False),
    sa.Column('lat', sa.Float(), nullable=False),
    sa.Column('lon', sa.Float(), nullable=False),
    sa.Column('soc', sa.Float(), nullable=False),
    sa.Column('tempo_kmh', sa.Float(), nullable=True),
    sa.Column('aussentemp_c', sa.Float(), nullable=True),
    sa.Column('km_auf_route', sa.Float(), nullable=True),
    sa.Column('soll_soc', sa.Float(), nullable=True),
    sa.ForeignKeyConstraint(['sitzung_id'], ['live_sitzungen.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('live_punkte', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_live_punkte_sitzung_id'), ['sitzung_id'], unique=False)



def downgrade() -> None:
    with op.batch_alter_table('live_punkte', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_live_punkte_sitzung_id'))

    op.drop_table('live_punkte')
    with op.batch_alter_table('live_sitzungen', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_live_sitzungen_fahrt_id'))

    op.drop_table('live_sitzungen')
    with op.batch_alter_table('ladekurvenpunkte', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_ladekurvenpunkte_fahrzeug_id'))

    op.drop_table('ladekurvenpunkte')
    op.drop_table('fahrten')
    with op.batch_alter_table('sitzungen', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_sitzungen_token'))

    op.drop_table('sitzungen')
    with op.batch_alter_table('ladepunkte', schema=None) as batch_op:
        batch_op.drop_index('ix_ladepunkt_pos')
        batch_op.drop_index('ix_ladepunkt_kw')

    op.drop_table('ladepunkte')
    op.drop_table('fahrzeuge')
