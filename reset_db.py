import asyncio
import asyncpg

async def reset_db():
    conn = await asyncpg.connect(
        user='',
        password='',
        database='',
        host='',
        port=5308
    )
    
    try:
        # Drop all tables by dropping schema
        await conn.execute("DROP SCHEMA IF EXISTS public CASCADE")
        print('✓ Dropped public schema')
        
        # Recreate schema
        await conn.execute("CREATE SCHEMA public")
        print('✓ Recreated public schema')
        
    finally:
        await conn.close()

asyncio.run(reset_db())
