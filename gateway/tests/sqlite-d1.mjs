// D1 batches execute sequentially in one transaction. Keep the local fixture
// synchronous inside BEGIN/COMMIT so concurrent callers cannot interleave it.
export function sqliteD1(sql){
 return {
  prepare(query){
   const stmt=sql.prepare(query);let args=[];
   return {bind(...values){args=values;return this},execute(){return stmt.run(...args)},async first(){return stmt.get(...args)},async run(){return stmt.run(...args)},async all(){return {results:stmt.all(...args)}}};
  },
  async batch(statements){
   sql.exec('BEGIN');
   try{const results=statements.map(s=>s.execute());sql.exec('COMMIT');return results}catch(error){sql.exec('ROLLBACK');throw error}
  },
 };
}
